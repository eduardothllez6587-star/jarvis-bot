import os
import re
import json
import logging
import requests
import tempfile
from pathlib import Path
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from groq import Groq

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
MEMORY_FILE    = "memory.json"
MODEL          = "llama-3.1-8b-instant"

groq_client = Groq(api_key=GROQ_API_KEY)

# ── Memoria ───────────────────────────────────────────────────────────────────
def load_memory() -> dict:
    if Path(MEMORY_FILE).exists():
        try:
            return json.loads(Path(MEMORY_FILE).read_text())
        except Exception:
            pass
    return {}

def save_memory(data: dict):
    Path(MEMORY_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2))

def get_user_memory(user_id: int) -> dict:
    mem = load_memory()
    return mem.get(str(user_id), {"items": [], "history": []})

def update_user_memory(user_id: int, user_mem: dict):
    mem = load_memory()
    mem[str(user_id)] = user_mem
    save_memory(mem)

def extract_memory_tag(text: str):
    """Extrae <<<MEMORIA:{...}>>> del texto si existe."""
    match = re.search(r"<<<MEMORIA:(\{.*?\})>>>", text, re.DOTALL)
    if match:
        try:
            item = json.loads(match.group(1))
            clean = text[:match.start()].strip() + text[match.end():].strip()
            return clean.strip(), item
        except Exception:
            pass
    return text, None

# ── Detección de agente ───────────────────────────────────────────────────────
def detect_agent(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["imagen", "foto", "dibuja", "genera", "pinta", "anime", "ilustra"]):
        return "imagen"
    if any(w in t for w in ["codigo", "código", "python", "programar", "script", "función", "funcion", "bug"]):
        return "codigo"
    if any(w in t for w in ["escribe", "historia", "cuento", "poema", "redacta", "carta", "correo", "email"]):
        return "escribe"
    if any(w in t for w in ["word", "documento", ".docx"]):
        return "word"
    if any(w in t for w in ["excel", "hoja", "tabla", ".xlsx", "spreadsheet"]):
        return "excel"
    if any(w in t for w in ["powerpoint", "presentacion", "presentación", "slides", ".pptx"]):
        return "pptx"
    return "director"

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPTS = {
    "director": (
        "Eres Jarvis, el asistente personal élite de Eduardo. "
        "Estilo J.A.R.V.I.S. de Tony Stark: inteligente, directo, ocasionalmente ingenioso, nunca verboso. "
        "Siempre responde en español. "
        "Si el usuario menciona proyectos, tareas o preferencias importantes, termina tu respuesta con: "
        '<<<MEMORIA:{"tipo":"proyecto/preferencia/tarea","dato":"descripción breve"}>>>'
    ),
    "codigo": (
        "Eres Jarvis en modo ingeniero senior. "
        "Escribe código limpio y explica brevemente tus decisiones. "
        "Usa comentarios claros. Responde en español."
    ),
    "escribe": (
        "Eres Jarvis en modo escritor maestro. "
        "Adaptas tu estilo a cualquier género: técnico, creativo, persuasivo, poético. "
        "Tienes alma y criterio propio. Responde en español."
    ),
    "imagen": (
        "Eres Jarvis en modo director visual. "
        "El usuario quiere generar una imagen. "
        "Toma su descripción y devuelve SOLO un prompt optimizado en inglés para generación de imagen, "
        "sin explicaciones, sin comillas, sin texto adicional. "
        "El prompt debe ser descriptivo, específico y cinematográfico."
    ),
    "word": (
        "Eres Jarvis en modo asistente de documentos. "
        "El usuario quiere crear un documento Word. "
        "Genera el contenido completo y bien estructurado en español. "
        "Usa títulos con # y ## para secciones. Sé profesional y detallado."
    ),
    "excel": (
        "Eres Jarvis en modo analista. "
        "El usuario quiere crear una hoja de cálculo. "
        "Devuelve los datos en formato CSV limpio, con encabezados claros. "
        "Responde SOLO con el CSV, sin explicaciones adicionales."
    ),
    "pptx": (
        "Eres Jarvis en modo director de presentaciones. "
        "El usuario quiere crear una presentación PowerPoint. "
        "Genera el contenido slide por slide en formato JSON así: "
        '[{"titulo":"Slide 1","puntos":["punto 1","punto 2"]}, ...] '
        "Devuelve SOLO el JSON, sin texto adicional."
    ),
}

# ── Groq ──────────────────────────────────────────────────────────────────────
def ask_groq(agent: str, user_message: str, history: list, memory_items: list) -> str:
    system = SYSTEM_PROMPTS.get(agent, SYSTEM_PROMPTS["director"])
    if memory_items:
        system += "\n\nMemoria de conversaciones previas con Eduardo:\n"
        system += "\n".join(f"- [{m.get('tipo','')}] {m.get('dato','')}" for m in memory_items)

    messages = [{"role": "system", "content": system}]
    # Últimos 10 turnos para contexto
    messages += history[-10:]
    messages.append({"role": "user", "content": user_message})

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )
    return response.choices[0].message.content.strip()

# ── Imagen ────────────────────────────────────────────────────────────────────
def generate_image_url(prompt: str) -> str:
    encoded = requests.utils.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={hash(prompt) % 99999}"

# ── Crear Word ────────────────────────────────────────────────────────────────
def create_word_doc(content: str, filename: str = "documento.docx") -> str:
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        # Estilo general
        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(11)

        for line in content.split("\n"):
            line = line.strip()
            if not line:
                doc.add_paragraph()
                continue
            if line.startswith("## "):
                p = doc.add_heading(line[3:], level=2)
            elif line.startswith("# "):
                p = doc.add_heading(line[2:], level=1)
            elif line.startswith("- "):
                doc.add_paragraph(line[2:], style="List Bullet")
            else:
                doc.add_paragraph(line)

        path = f"/tmp/{filename}"
        doc.save(path)
        return path
    except ImportError:
        return None

# ── Crear Excel ───────────────────────────────────────────────────────────────
def create_excel(csv_content: str, filename: str = "datos.xlsx") -> str:
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Datos"

        lines = [l for l in csv_content.strip().split("\n") if l.strip()]
        for row_idx, line in enumerate(lines, 1):
            cells = [c.strip().strip('"') for c in line.split(",")]
            for col_idx, val in enumerate(cells, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                if row_idx == 1:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="1D9E75")
                    cell.alignment = Alignment(horizontal="center")

        # Auto-ancho
        for col in ws.columns:
            max_len = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

        path = f"/tmp/{filename}"
        wb.save(path)
        return path
    except ImportError:
        return None

# ── Crear PowerPoint ──────────────────────────────────────────────────────────
def create_pptx(slides_json: str, filename: str = "presentacion.pptx") -> str:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
        import json

        slides_data = json.loads(slides_json)
        prs = Presentation()
        prs.slide_width  = Inches(13.33)
        prs.slide_height = Inches(7.5)

        for slide_data in slides_data:
            layout = prs.slide_layouts[1]  # título + contenido
            slide  = prs.slides.add_slide(layout)
            title  = slide.shapes.title
            body   = slide.placeholders[1]

            title.text = slide_data.get("titulo", "")
            title.text_frame.paragraphs[0].font.size = Pt(32)
            title.text_frame.paragraphs[0].font.bold = True
            title.text_frame.paragraphs[0].font.color.rgb = RGBColor(0x1D, 0x9E, 0x75)

            tf = body.text_frame
            tf.clear()
            for i, punto in enumerate(slide_data.get("puntos", [])):
                p = tf.add_paragraph() if i > 0 else tf.paragraphs[0]
                p.text = punto
                p.font.size = Pt(20)
                p.level = 0

        path = f"/tmp/{filename}"
        prs.save(path)
        return path
    except (ImportError, json.JSONDecodeError):
        return None

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hola Eduardo, soy Jarvis.*\n\n"
        "Puedo ayudarte con:\n"
        "🖼 Imágenes — _'genera una imagen de...'_\n"
        "📝 Documentos Word — _'crea un word sobre...'_\n"
        "📊 Excel — _'haz una tabla con...'_\n"
        "📊 PowerPoint — _'crea una presentación de...'_\n"
        "💬 Conversación general — pregúntame lo que sea\n\n"
        "¿En qué te ayudo?",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    message  = update.message.text or ""
    agent    = detect_agent(message)
    user_mem = get_user_memory(user_id)

    # Indicador de escritura
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = ask_groq(agent, message, user_mem.get("history", []), user_mem.get("items", []))
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text("❌ Error al contactar la IA. Intenta de nuevo.")
        return

    # Guardar en historial
    user_mem.setdefault("history", [])
    user_mem["history"].append({"role": "user",      "content": message})
    user_mem["history"].append({"role": "assistant", "content": reply})
    # Máximo 40 turnos en historial
    user_mem["history"] = user_mem["history"][-40:]

    # ── Agente imagen ──
    if agent == "imagen":
        image_url = generate_image_url(reply)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        try:
            img_response = requests.get(image_url, timeout=30)
            if img_response.status_code == 200:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    f.write(img_response.content)
                    tmp_path = f.name
                with open(tmp_path, "rb") as f:
                    await update.message.reply_photo(photo=f, caption=f"🎨 _{reply[:200]}_", parse_mode="Markdown")
                Path(tmp_path).unlink(missing_ok=True)
            else:
                await update.message.reply_text("❌ No pude generar la imagen. Intenta con otra descripción.")
        except Exception as e:
            logger.error(f"Image error: {e}")
            await update.message.reply_text("❌ Error generando imagen.")
        update_user_memory(user_id, user_mem)
        return

    # ── Agente Word ──
    if agent == "word":
        clean_reply, mem_item = extract_memory_tag(reply)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_document")
        fname = f"Jarvis_{datetime.now().strftime('%Y%m%d_%H%M')}.docx"
        path  = create_word_doc(clean_reply, fname)
        if path:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f, filename=fname, caption="📄 Aquí está tu documento Word.")
            Path(path).unlink(missing_ok=True)
        else:
            await update.message.reply_text(clean_reply)
        if mem_item:
            user_mem.setdefault("items", []).append(mem_item)
        update_user_memory(user_id, user_mem)
        return

    # ── Agente Excel ──
    if agent == "excel":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_document")
        fname = f"Jarvis_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        path  = create_excel(reply, fname)
        if path:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f, filename=fname, caption="📊 Aquí está tu Excel.")
            Path(path).unlink(missing_ok=True)
        else:
            await update.message.reply_text(reply)
        update_user_memory(user_id, user_mem)
        return

    # ── Agente PowerPoint ──
    if agent == "pptx":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_document")
        fname = f"Jarvis_{datetime.now().strftime('%Y%m%d_%H%M')}.pptx"
        path  = create_pptx(reply, fname)
        if path:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f, filename=fname, caption="📊 Aquí está tu presentación.")
            Path(path).unlink(missing_ok=True)
        else:
            await update.message.reply_text("Contenido de la presentación:\n\n" + reply)
        update_user_memory(user_id, user_mem)
        return

    # ── Director / Código / Escribe ──
    clean_reply, mem_item = extract_memory_tag(reply)
    if mem_item:
        user_mem.setdefault("items", []).append(mem_item)
    update_user_memory(user_id, user_mem)
    await update.message.reply_text(clean_reply)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe documentos y los resume."""
    doc  = update.message.document
    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(doc.file_name).suffix) as f:
        await file.download_to_drive(f.name)
        tmp_path = f.name

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Leer texto del archivo
    content = ""
    try:
        if doc.file_name.endswith(".txt"):
            content = Path(tmp_path).read_text(errors="ignore")[:4000]
        elif doc.file_name.endswith(".pdf"):
            import pdfplumber
            with pdfplumber.open(tmp_path) as pdf:
                content = "\n".join(p.extract_text() or "" for p in pdf.pages)[:4000]
        elif doc.file_name.endswith(".docx"):
            from docx import Document
            d = Document(tmp_path)
            content = "\n".join(p.text for p in d.paragraphs)[:4000]
    except Exception as e:
        content = ""
        logger.error(f"Doc read error: {e}")

    Path(tmp_path).unlink(missing_ok=True)

    if not content:
        await update.message.reply_text("📎 Recibí el archivo pero no pude leer su contenido. Intenta con .txt, .pdf o .docx.")
        return

    user_id  = update.effective_user.id
    user_mem = get_user_memory(user_id)
    prompt   = f"El usuario me envió el documento '{doc.file_name}'. Aquí está el contenido:\n\n{content}\n\nResúmelo y pregunta qué quiere hacer con él."
    try:
        reply = ask_groq("director", prompt, user_mem.get("history", []), user_mem.get("items", []))
        clean_reply, _ = extract_memory_tag(reply)
        await update.message.reply_text(clean_reply)
    except Exception as e:
        await update.message.reply_text("❌ Error procesando el documento.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Jarvis iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
