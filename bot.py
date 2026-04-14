import os, json, re, logging, tempfile, asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from google import genai
import edge_tts

# ── Config ──
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_KEY"]
TZ = ZoneInfo("Europe/Budapest")
DATA = "data.json"

client = genai.Client(api_key=GEMINI_KEY)


# ═══════════════════════════════════
#  STORAGE — simple JSON persistence
# ═══════════════════════════════════
def load():
    try:
        with open(DATA) as f:
            return json.load(f)
    except Exception:
        return {}

def save(d):
    with open(DATA, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def uget(uid):
    d = load()
    uid = str(uid)
    if uid not in d:
        d[uid] = {
            "level": "A2.1", "hour": 9, "minute": 0,
            "streak": 0, "last": None, "topics": [],
            "chat": [],
            "lesson": None, "phase": None,
            "q_idx": 0, "score_l": 0, "score_r": 0
        }
        save(d)
    return d[uid]

def uset(uid, **kw):
    d = load()
    uid = str(uid)
    if uid not in d:
        uget(uid)
        d = load()
    d[uid].update(kw)
    save(d)


# ═══════════════════════
#  TTS — German audio
# ═══════════════════════
async def make_audio(text):
    path = tempfile.mktemp(suffix=".mp3")
    comm = edge_tts.Communicate(text, "de-DE-ConradNeural", rate="-10%")
    await comm.save(path)
    return path


# ═══════════════════════════════
#  GEMINI — lesson generation
# ═══════════════════════════════
async def gen_lesson(level, done_topics):
    avoid = ", ".join(done_topics[-30:]) if done_topics else "none yet"

    prompt = f"""Generate a German lesson for a student at level {level}.

Create a SHORT German text (4-6 simple sentences) about an everyday topic.
Topics already done (AVOID these): {avoid}

Pick from real-life topics like: beim Arzt, im Supermarkt, Wohnung suchen,
am Bahnhof, im Restaurant bestellen, Wochenendplaene, mein Hobby, eine Reise planen,
auf der Arbeit, meine Nachbarn, das Wetter, Sport treiben, meine Familie,
Geburtstag feiern, im Cafe, Haustiere, Umzug, mit dem Bus fahren, Kleidung kaufen,
Fruehstueck, Arzttermin machen, Handy-Probleme, im Park, Deutsch lernen...

Return ONLY valid JSON (no markdown, no ```):
{{
  "topic": "topic in English",
  "text": "German text, {level} level, 4-6 sentences",
  "listening_qs": [
    {{"q": "question about audio in English", "opts": ["A", "B", "C"], "ans": 0, "why": "explanation in English"}},
    {{"q": "question 2", "opts": ["A", "B", "C"], "ans": 1, "why": "explanation"}},
    {{"q": "question 3", "opts": ["A", "B", "C"], "ans": 2, "why": "explanation"}}
  ],
  "reading_qs": [
    {{"q": "True/False statement in English", "opts": ["True", "False", "Not Given"], "ans": 0, "why": "explanation"}},
    {{"q": "statement 2", "opts": ["True", "False", "Not Given"], "ans": 1, "why": "explanation"}},
    {{"q": "statement 3", "opts": ["True", "False", "Not Given"], "ans": 2, "why": "explanation"}}
  ],
  "phrases": ["useful phrase from text 1", "phrase 2", "phrase 3", "phrase 4"],
  "vocab": [
    {{"de": "German word/phrase", "en": "English", "ru": "Russian"}},
    {{"de": "word 2", "en": "English", "ru": "Russian"}},
    {{"de": "word 3", "en": "English", "ru": "Russian"}},
    {{"de": "word 4", "en": "English", "ru": "Russian"}},
    {{"de": "word 5", "en": "English", "ru": "Russian"}}
  ]
}}"""

    resp = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.0-flash-lite",
        contents=prompt
    )
    txt = resp.text.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```\w*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt)
    return json.loads(txt)


async def chat_reply(msg, level, history):
    system = f"""You are a friendly German tutor for a {level} student.

Rules:
- If they write in German: reply in German, then add corrections/tips in English below
- If they ask in English: explain in English with German examples
- Correct grammar mistakes gently
- Keep responses under 150 words
- Be encouraging and natural
- Match vocabulary to their {level} level"""

    contents = []
    for h in history[-10:]:
        contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
    contents.append({"role": "user", "parts": [{"text": msg}]})

    resp = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.0-flash-lite",
        contents=contents,
        config={"system_instruction": system}
    )
    return resp.text


# ══════════════════════════
#  LESSON FLOW
# ══════════════════════════
async def do_lesson(bot, chat_id, uid):
    u = uget(uid)
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    await bot.send_message(chat_id, "⏳ Generating your lesson...")

    try:
        lesson = await gen_lesson(u["level"], u.get("topics", []))
    except Exception as e:
        log.error(f"Gemini error: {e}")
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            await bot.send_message(chat_id,
                "⏳ Daily API limit reached. Resets around 9:00 AM Budapest time.\n"
                "Try again later!")
        else:
            await bot.send_message(chat_id, "❌ Lesson generation failed. Try /lesson again.")
        return

    # Save state
    topics = u.get("topics", [])
    topics.append(lesson["topic"])
    streak = u["streak"]
    if u.get("last") != today:
        streak += 1
    uset(uid,
         lesson=lesson, phase="listening", q_idx=0,
         score_l=0, score_r=0,
         last=today, streak=streak, topics=topics[-50:])

    # Send listening intro + audio
    await bot.send_message(chat_id,
        "🎧 <b>Listening Exercise</b>\n\n"
        f"Topic: <i>{lesson['topic']}</i>\n"
        "Listen carefully, then answer the questions.",
        parse_mode="HTML")

    try:
        audio = await make_audio(lesson["text"])
        with open(audio, "rb") as f:
            await bot.send_voice(chat_id, f)
        os.unlink(audio)
    except Exception as e:
        log.error(f"TTS error: {e}")
        await bot.send_message(chat_id, "⚠️ Audio unavailable, moving to questions...")

    await send_q(bot, chat_id, uid)


async def send_q(bot, chat_id, uid):
    u = uget(uid)
    lesson = u.get("lesson")
    if not lesson:
        return

    phase = u["phase"]
    idx = u["q_idx"]
    qs = lesson["listening_qs"] if phase == "listening" else lesson["reading_qs"]

    if idx >= len(qs):
        if phase == "listening":
            # Transition to reading
            uset(uid, phase="reading", q_idx=0)
            text = lesson["text"]
            await bot.send_message(chat_id,
                "📖 <b>Reading Exercise</b>\n\n"
                f"Now read the text:\n\n<i>{text}</i>\n\n"
                "Answer the questions below.",
                parse_mode="HTML")
            await send_q(bot, chat_id, uid)
        else:
            # Done — show results
            await send_results(bot, chat_id, uid)
        return

    q = qs[idx]
    prefix = "lq" if phase == "listening" else "rq"
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"{prefix}_{idx}_{i}")]
        for i, opt in enumerate(q["opts"])
    ]
    emoji = "🎧" if phase == "listening" else "📖"
    await bot.send_message(
        chat_id,
        f"{emoji} <b>Q{idx + 1}:</b> {q['q']}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def send_results(bot, chat_id, uid):
    u = uget(uid)
    lesson = u["lesson"]
    sl, sr = u["score_l"], u["score_r"]
    total = sl + sr
    pct = round(total / 6 * 100)

    bar_len = 10
    filled = round(total / 6 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)

    phrases = "\n".join(f"  🗣️ <i>{p}</i>" for p in lesson["phrases"])
    vocab = "\n".join(
        f"  • <b>{v['de']}</b> — {v['en']} / {v['ru']}"
        for v in lesson["vocab"]
    )

    await bot.send_message(chat_id,
        f"📊 <b>Results</b>\n"
        f"🎧 Listening: {sl}/3\n"
        f"📖 Reading: {sr}/3\n"
        f"Total: {total}/6 ({pct}%)\n"
        f"{bar}\n\n"
        f"🗣️ <b>Speaking Practice</b>\n"
        f"Say these out loud:\n{phrases}\n\n"
        f"📝 <b>New Vocabulary</b>\n{vocab}\n\n"
        f"🔥 Streak: {u['streak']} days\n\n"
        f"💬 Want to practice more? Just write me in German!",
        parse_mode="HTML")

    # Try to generate audio for speaking phrases
    try:
        phrases_text = ". ".join(lesson["phrases"])
        audio = await make_audio(phrases_text)
        with open(audio, "rb") as f:
            await bot.send_voice(chat_id, f, caption="🗣️ Listen and repeat these phrases")
        os.unlink(audio)
    except Exception:
        pass

    # Clear lesson state
    uset(uid, lesson=None, phase=None, q_idx=0, score_l=0, score_r=0)


# ═══════════════════════
#  COMMAND HANDLERS
# ═══════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = uget(update.effective_user.id)
    await update.message.reply_text(
        "🇩🇪 <b>Willkommen!</b> Welcome to DeutschDaily!\n\n"
        "I'll help you practice German every day:\n"
        "🎧 Listening + quiz\n"
        "📖 Reading + quiz\n"
        "🗣️ Speaking phrases + vocab\n"
        "💬 Chat practice anytime\n\n"
        f"📊 Level: <b>{u['level']}</b>\n"
        f"⏰ Daily lesson: <b>{u['hour']:02d}:{u['minute']:02d}</b> (Budapest)\n\n"
        "<b>Commands:</b>\n"
        "/lesson — get a lesson now\n"
        "/level — change your level\n"
        "/time — set daily lesson time\n"
        "/streak — view your streak\n"
        "/help — show all commands\n\n"
        "Or just <b>write me in German</b> anytime for conversation practice!",
        parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Commands</b>\n\n"
        "/lesson — get a lesson right now\n"
        "/level — set your German level\n"
        "/time — set daily lesson time\n"
        "/streak — view your learning streak\n"
        "/skip — skip current quiz\n"
        "/help — this message\n\n"
        "💬 <b>Chat mode:</b> just send any text message "
        "in German or English — I'll respond, correct, and teach!",
        parse_mode="HTML")


async def cmd_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("A1", callback_data="lvl_A1"),
         InlineKeyboardButton("A1.2", callback_data="lvl_A1.2"),
         InlineKeyboardButton("A2.1", callback_data="lvl_A2.1")],
        [InlineKeyboardButton("A2.2", callback_data="lvl_A2.2"),
         InlineKeyboardButton("B1.1", callback_data="lvl_B1.1"),
         InlineKeyboardButton("B1.2", callback_data="lvl_B1.2")]
    ])
    await update.message.reply_text("Select your level:", reply_markup=kb)


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("07:00", callback_data="tm_7_0"),
         InlineKeyboardButton("08:00", callback_data="tm_8_0"),
         InlineKeyboardButton("09:00", callback_data="tm_9_0")],
        [InlineKeyboardButton("10:00", callback_data="tm_10_0"),
         InlineKeyboardButton("12:00", callback_data="tm_12_0"),
         InlineKeyboardButton("14:00", callback_data="tm_14_0")],
        [InlineKeyboardButton("17:00", callback_data="tm_17_0"),
         InlineKeyboardButton("19:00", callback_data="tm_19_0"),
         InlineKeyboardButton("21:00", callback_data="tm_21_0")]
    ])
    await update.message.reply_text(
        "⏰ When should I send your daily lesson?\n(Budapest time)",
        reply_markup=kb)


async def cmd_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = uget(update.effective_user.id)
    await update.message.reply_text(
        f"🔥 Streak: <b>{u['streak']}</b> days\n"
        f"📚 Topics covered: <b>{len(u.get('topics', []))}</b>\n"
        f"📊 Level: <b>{u['level']}</b>",
        parse_mode="HTML")


async def cmd_lesson(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = uget(uid)
    if u.get("lesson"):
        await update.message.reply_text(
            "📝 You already have an active lesson!\n"
            "Finish the quiz or use /skip to start a new one.")
        return
    await do_lesson(ctx.bot, update.effective_chat.id, uid)


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uset(uid, lesson=None, phase=None, q_idx=0, score_l=0, score_r=0)
    await update.message.reply_text("⏭️ Quiz skipped. Use /lesson to start a new one.")


# ══════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    # Level selection
    if data.startswith("lvl_"):
        level = data[4:]
        uset(uid, level=level)
        await query.edit_message_text(f"✅ Level set to <b>{level}</b>", parse_mode="HTML")
        return

    # Time selection
    if data.startswith("tm_"):
        parts = data.split("_")
        hour, minute = int(parts[1]), int(parts[2])
        uset(uid, hour=hour, minute=minute)
        schedule_user(ctx.application, uid, hour, minute)
        await query.edit_message_text(
            f"✅ Daily lesson set to <b>{hour:02d}:{minute:02d}</b> Budapest time",
            parse_mode="HTML")
        return

    # Quiz answers
    if data.startswith("lq_") or data.startswith("rq_"):
        phase_key = "listening" if data.startswith("lq_") else "reading"
        parts = data.split("_")
        q_idx = int(parts[1])
        chosen = int(parts[2])

        u = uget(uid)
        lesson = u.get("lesson")
        if not lesson:
            await query.edit_message_text("⚠️ Session expired. Use /lesson to start new.")
            return

        qs = lesson["listening_qs"] if phase_key == "listening" else lesson["reading_qs"]
        if q_idx >= len(qs):
            return

        q = qs[q_idx]
        correct = chosen == q["ans"]
        score_key = "score_l" if phase_key == "listening" else "score_r"

        if correct:
            uset(uid, **{score_key: u[score_key] + 1})

        emoji = "✅" if correct else "❌"
        correct_opt = q["opts"][q["ans"]]
        icon = "🎧" if phase_key == "listening" else "📖"

        await query.edit_message_text(
            f"{icon} <b>Q{q_idx + 1}:</b> {q['q']}\n\n"
            f"{emoji} {'Correct!' if correct else f'Answer: <b>{correct_opt}</b>'}\n"
            f"💡 {q['why']}",
            parse_mode="HTML")

        # Next question
        uset(uid, q_idx=q_idx + 1)
        chat_id = query.message.chat_id
        await send_q(ctx.bot, chat_id, uid)


# ═══════════════════════════
#  CHAT MODE — free practice
# ═══════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = uget(uid)

    # If in lesson, remind to finish
    if u.get("lesson"):
        await update.message.reply_text(
            "📝 You have an active lesson! Finish the quiz first.\n"
            "Or use /skip to start fresh.")
        return

    user_text = update.message.text
    if not user_text:
        return

    try:
        history = u.get("chat", [])
        reply = await chat_reply(user_text, u["level"], history)

        # Save chat history
        history.append({"role": "user", "text": user_text})
        history.append({"role": "model", "text": reply})
        uset(uid, chat=history[-20:])

        await update.message.reply_text(reply)
    except Exception as e:
        log.error(f"Chat error: {e}")
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            await update.message.reply_text(
                "⏳ Daily API limit reached. Resets around 9:00 AM Budapest time.\n"
                "Try again later — or use /lesson in the morning!")
        else:
            await update.message.reply_text("❌ Something went wrong. Try again!")


# ═══════════════════════════
#  SCHEDULER — daily lessons
# ═══════════════════════════
def schedule_user(app, uid, hour, minute):
    job_id = f"daily_{uid}"
    # Remove existing job
    existing = app.job_queue.get_jobs_by_name(job_id)
    for job in existing:
        job.schedule_removal()
    # Add new daily job
    app.job_queue.run_daily(
        daily_job,
        time=dtime(hour=hour, minute=minute, tzinfo=TZ),
        data={"uid": uid, "chat_id": uid},
        name=job_id
    )
    log.info(f"Scheduled daily lesson for {uid} at {hour:02d}:{minute:02d}")


async def daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.data["uid"]
    chat_id = ctx.job.data["chat_id"]
    u = uget(uid)
    if u.get("lesson"):
        # Clear stale lesson before sending new one
        uset(uid, lesson=None, phase=None, q_idx=0, score_l=0, score_r=0)
    await do_lesson(ctx.bot, chat_id, uid)


async def post_init(app: Application):
    """Schedule daily jobs for all existing users on startup."""
    data = load()
    for uid, u in data.items():
        schedule_user(app, int(uid), u.get("hour", 9), u.get("minute", 0))
    log.info(f"Scheduled {len(data)} users on startup")


# ═══════════
#  MAIN
# ═══════════
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lesson", cmd_lesson))
    app.add_handler(CommandHandler("level", cmd_level))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
