import os, json, re, logging, tempfile, asyncio, random
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
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA = os.path.join(DATA_DIR, "data.json")

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
            "chat": [], "history": [],
            "lesson": None, "phase": None,
            "q_idx": 0, "score_l": 0, "score_r": 0,
            "v_idx": 0, "score_v": 0, "wrote": False
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
#  GEMINI — AI calls
# ═══════════════════════════════
async def gemini_call(prompt, system=None, history=None):
    if history:
        contents = []
        for h in history[-10:]:
            contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})
    else:
        contents = prompt

    config = {"system_instruction": system} if system else None

    for attempt in range(3):
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-flash-lite",
                contents=contents,
                **({"config": config} if config else {})
            )
            return resp.text
        except Exception as e:
            err = str(e)
            if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt < 2:
                log.warning(f"Rate limited, retrying in {5 * (attempt + 1)}s...")
                await asyncio.sleep(5 * (attempt + 1))
                continue
            raise


async def gen_lesson(level, done_topics):
    avoid = ", ".join(done_topics[-30:]) if done_topics else "none yet"

    prompt = f"""Generate a German lesson for a student at level {level}.

Create a German text (8-12 sentences) about an INTERESTING topic.
Topics already done (AVOID these): {avoid}

Pick from diverse, engaging topics like:
- Science: Warum ist der Himmel blau, wie funktioniert das Internet, Planeten im Sonnensystem
- History: Die Berliner Mauer, Erfindung des Buchdrucks, die Seidenstraße
- Culture: Karneval in Deutschland, Wiener Kaffeehauskultur, deutsche Erfinder
- Technology: Künstliche Intelligenz einfach erklärt, Elektroautos, soziale Medien
- Nature: Warum Bäume die Blätter verlieren, das Wetter verstehen, Tiere im Winter
- Travel: Eine Reise nach Wien, mit dem Zug durch die Schweiz, Städte am Rhein
- Daily life: Einkaufen auf dem Markt, ein Tag im Büro, Umzug in eine neue Stadt
- Health: Gesund essen, Sport und Gesundheit, guter Schlaf
- Psychology: Warum wir Musik mögen, Gewohnheiten ändern, Motivation finden

The text should be INFORMATIVE and teach something interesting, not just describe a routine.
Use {level} level vocabulary but introduce 2-3 new harder words (explain them in vocab).

Return ONLY valid JSON (no markdown, no ```):
{{
  "topic": "topic in English",
  "text": "German text, {level} level, 8-12 sentences, informative and interesting",
  "listening_qs": [
    {{"q": "question about the audio content in English", "opts": ["A", "B", "C"], "ans": 0, "why": "brief explanation in English"}},
    {{"q": "question 2", "opts": ["A", "B", "C"], "ans": 1, "why": "explanation"}},
    {{"q": "question 3", "opts": ["A", "B", "C"], "ans": 2, "why": "explanation"}}
  ],
  "reading_qs": [
    {{"q": "True/False/NG statement about the text in English", "opts": ["True", "False", "Not Given"], "ans": 0, "why": "explanation referencing the text"}},
    {{"q": "statement 2", "opts": ["True", "False", "Not Given"], "ans": 1, "why": "explanation"}},
    {{"q": "statement 3", "opts": ["True", "False", "Not Given"], "ans": 2, "why": "explanation"}}
  ],
  "writing_prompt": "A question or task in English asking the student to write 3-5 sentences in German related to the topic. Should encourage personal opinion or experience.",
  "phrases": ["useful phrase from text 1", "phrase 2", "phrase 3", "phrase 4", "phrase 5", "phrase 6"],
  "vocab": [
    {{"de": "German word/phrase", "en": "English", "ru": "Russian"}},
    {{"de": "word 2", "en": "English", "ru": "Russian"}},
    {{"de": "word 3", "en": "English", "ru": "Russian"}},
    {{"de": "word 4", "en": "English", "ru": "Russian"}},
    {{"de": "word 5", "en": "English", "ru": "Russian"}},
    {{"de": "word 6", "en": "English", "ru": "Russian"}},
    {{"de": "word 7", "en": "English", "ru": "Russian"}}
  ]
}}"""

    txt = await gemini_call(prompt)
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```\w*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt)
    return json.loads(txt)


async def check_writing(text, topic, level):
    prompt = f"""A {level} German student wrote this text about "{topic}":

"{text}"

Review their writing. Be encouraging but thorough. Format your response like this:

📝 **Your text:**
(quote their text)

✅ **What you did well:**
- (1-2 positive points)

❌ **Corrections:**
- (list each grammar/spelling mistake with the fix)
- Format: "❌ [wrong] → ✅ [correct]" with brief explanation

📖 **Improved version:**
(rewrite their text with all corrections applied)

💡 **Tips:**
- (1-2 tips for improvement at their level)

Keep it concise and educational. Use English for explanations, German for examples."""

    return await gemini_call(prompt)


async def chat_reply(msg, level, history):
    system = f"""You are a friendly German tutor for a {level} student.

Rules:
- If they write in German: reply in German, then add corrections/tips in English below
- If they ask in English: explain in English with German examples
- Correct grammar mistakes gently
- Keep responses under 150 words
- Be encouraging and natural
- Match vocabulary to their {level} level"""

    return await gemini_call(msg, system=system, history=history)


# ══════════════════════════════
#  VOCAB QUIZ — inline keyboard
# ══════════════════════════════
def prepare_vocab_quiz(vocab):
    """Build quiz: for each word, show German → pick correct English from 4 options."""
    quiz = []
    all_en = [v["en"] for v in vocab]
    for i, word in enumerate(vocab):
        correct = word["en"]
        others = [e for j, e in enumerate(all_en) if j != i]
        random.shuffle(others)
        wrong = others[:3]
        opts = [correct] + wrong
        random.shuffle(opts)
        quiz.append({
            "de": word["de"],
            "opts": opts,
            "ans": opts.index(correct)
        })
    return quiz


async def send_vq(bot, chat_id, uid):
    """Send next vocab quiz question."""
    u = uget(uid)
    lesson = u.get("lesson")
    if not lesson or "vocab_quiz" not in lesson:
        return

    idx = u.get("v_idx", 0)
    vq = lesson["vocab_quiz"]

    if idx >= len(vq):
        await finish_lesson(bot, chat_id, uid)
        return

    q = vq[idx]
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"vq_{idx}_{i}")]
        for i, opt in enumerate(q["opts"])
    ]
    await bot.send_message(
        chat_id,
        f"📝 <b>Vocab {idx + 1}/{len(vq)}:</b> What does <b>{q['de']}</b> mean?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def finish_lesson(bot, chat_id, uid):
    """Show final summary and save to history."""
    u = uget(uid)
    lesson = u["lesson"]
    sl = u.get("score_l", 0)
    sr = u.get("score_r", 0)
    sv = u.get("score_v", 0)
    vq_len = len(lesson.get("vocab_quiz", []))
    wrote = u.get("wrote", False)

    total = sl + sr + sv
    max_total = 6 + vq_len
    pct = round(total / max_total * 100) if max_total else 0

    bar_len = 10
    filled = round(total / max_total * bar_len) if max_total else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    # Save to history
    hist = u.get("history", [])
    hist.append({
        "date": datetime.now(TZ).strftime("%Y-%m-%d"),
        "topic": lesson["topic"],
        "listening": sl, "reading": sr, "vocab": sv,
        "vocab_total": vq_len, "writing": wrote
    })
    # Keep last 100 lessons
    hist = hist[-100:]

    await bot.send_message(chat_id,
        f"🏁 <b>Lesson Complete!</b>\n\n"
        f"📊 <b>Final Score: {total}/{max_total} ({pct}%)</b>\n"
        f"{bar}\n\n"
        f"🎧 Listening: {sl}/3\n"
        f"📖 Reading: {sr}/3\n"
        f"📝 Vocabulary: {sv}/{vq_len}\n"
        f"✍️ Writing: {'Done' if wrote else 'Skipped'}\n\n"
        f"🔥 Streak: {u['streak']} days\n"
        f"📚 Total lessons: {len(hist)}\n\n"
        f"💬 Chat with me in German or /lesson for another!",
        parse_mode="HTML")

    uset(uid, lesson=None, phase=None, q_idx=0,
         score_l=0, score_r=0, v_idx=0, score_v=0, wrote=False,
         history=hist)


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
                "⏳ API limit reached (free tier). Try again in 10-15 minutes.")
        else:
            await bot.send_message(chat_id, "❌ Lesson generation failed. Try /lesson again.")
        return

    topics = u.get("topics", [])
    topics.append(lesson["topic"])
    streak = u["streak"]
    if u.get("last") != today:
        streak += 1
    uset(uid,
         lesson=lesson, phase="listening", q_idx=0,
         score_l=0, score_r=0, v_idx=0, score_v=0, wrote=False,
         last=today, streak=streak, topics=topics[-50:])

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
            uset(uid, phase="reading", q_idx=0)
            text = lesson["text"]
            await bot.send_message(chat_id,
                "📖 <b>Reading Exercise</b>\n\n"
                f"Now read the text:\n\n<i>{text}</i>\n\n"
                "Answer the questions below.",
                parse_mode="HTML")
            await send_q(bot, chat_id, uid)
        else:
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
    """Show quiz results, vocab list, speaking phrases, then writing prompt."""
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
        f"📊 <b>Quiz Results</b>\n"
        f"🎧 Listening: {sl}/3\n"
        f"📖 Reading: {sr}/3\n"
        f"Total: {total}/6 ({pct}%)\n"
        f"{bar}\n\n"
        f"🗣️ <b>Speaking Practice</b>\n"
        f"Say these out loud:\n{phrases}\n\n"
        f"📝 <b>New Vocabulary</b>\n{vocab}",
        parse_mode="HTML")

    try:
        phrases_text = ". ".join(lesson["phrases"])
        audio = await make_audio(phrases_text)
        with open(audio, "rb") as f:
            await bot.send_voice(chat_id, f, caption="🗣️ Listen and repeat these phrases")
        os.unlink(audio)
    except Exception:
        pass

    # Writing prompt
    writing_prompt = lesson.get("writing_prompt",
        f"Write 3-5 sentences in German about your thoughts on '{lesson['topic']}'.")

    await bot.send_message(chat_id,
        f"✍️ <b>Writing Exercise</b>\n\n"
        f"{writing_prompt}\n\n"
        f"<i>Write your answer in German below. "
        f"I'll check your grammar, vocabulary, and give feedback!</i>\n\n"
        f"(or /skip to skip to vocab quiz)",
        parse_mode="HTML")

    uset(uid, phase="writing")


async def start_vocab_quiz(bot, chat_id, uid):
    """Prepare and start the vocab quiz."""
    u = uget(uid)
    lesson = u.get("lesson")
    if not lesson or not lesson.get("vocab"):
        await finish_lesson(bot, chat_id, uid)
        return

    vq = prepare_vocab_quiz(lesson["vocab"])
    lesson["vocab_quiz"] = vq
    uset(uid, lesson=lesson, phase="vocab_quiz", v_idx=0, score_v=0)

    await bot.send_message(chat_id,
        f"📝 <b>Vocabulary Quiz</b>\n\n"
        f"Let's test the {len(vq)} new words! Pick the correct meaning.",
        parse_mode="HTML")

    await send_vq(bot, chat_id, uid)


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
        "✍️ Writing with AI feedback\n"
        "📝 Vocabulary quiz\n"
        "💬 Chat practice anytime\n\n"
        f"📊 Level: <b>{u['level']}</b>\n"
        f"⏰ Daily lesson: <b>{u['hour']:02d}:{u['minute']:02d}</b> (Budapest)\n\n"
        "<b>Commands:</b>\n"
        "/lesson — get a lesson now\n"
        "/progress — view your stats\n"
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
        "/progress — view your learning stats\n"
        "/level — set your German level\n"
        "/time — set daily lesson time\n"
        "/streak — view your streak\n"
        "/skip — skip current exercise\n"
        "/help — this message\n\n"
        "💬 <b>Chat mode:</b> just send any text message "
        "in German or English — I'll respond, correct, and teach!",
        parse_mode="HTML")


async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = uget(update.effective_user.id)
    hist = u.get("history", [])

    if not hist:
        await update.message.reply_text(
            "📊 No lessons completed yet!\nUse /lesson to start your first one.")
        return

    total_lessons = len(hist)
    avg_l = sum(h.get("listening", 0) for h in hist) / total_lessons
    avg_r = sum(h.get("reading", 0) for h in hist) / total_lessons
    vocab_entries = [h for h in hist if h.get("vocab_total", 0) > 0]
    avg_v = (sum(h["vocab"] for h in vocab_entries) /
             sum(h["vocab_total"] for h in vocab_entries) * 100) if vocab_entries else 0
    writings = sum(1 for h in hist if h.get("writing"))

    # Recent 5 lessons
    recent = hist[-5:]
    recent_lines = []
    for h in reversed(recent):
        vt = h.get("vocab_total", 0)
        v_str = f" | V:{h.get('vocab', 0)}/{vt}" if vt else ""
        w_str = " ✍️" if h.get("writing") else ""
        recent_lines.append(
            f"  {h['date']} — <i>{h['topic']}</i>\n"
            f"    L:{h.get('listening', 0)}/3 | R:{h.get('reading', 0)}/3{v_str}{w_str}")

    recent_text = "\n".join(recent_lines)

    # Skill bars
    def bar(val, mx):
        pct = val / mx if mx else 0
        filled = round(pct * 8)
        return "█" * filled + "░" * (8 - filled) + f" {val:.1f}/{mx}"

    await update.message.reply_text(
        f"📊 <b>Your Progress</b>\n\n"
        f"📚 Total lessons: <b>{total_lessons}</b>\n"
        f"🔥 Current streak: <b>{u['streak']}</b> days\n"
        f"📊 Level: <b>{u['level']}</b>\n"
        f"✍️ Writings done: <b>{writings}</b>\n\n"
        f"<b>Average Scores</b>\n"
        f"🎧 Listening: {bar(avg_l, 3)}\n"
        f"📖 Reading:   {bar(avg_r, 3)}\n"
        f"📝 Vocab:     {avg_v:.0f}% correct\n\n"
        f"<b>Recent Lessons</b>\n{recent_text}",
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
        f"📚 Lessons completed: <b>{len(u.get('history', []))}</b>\n"
        f"📊 Level: <b>{u['level']}</b>",
        parse_mode="HTML")


async def cmd_lesson(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = uget(uid)
    if u.get("lesson"):
        phase = u.get("phase", "")
        if phase == "writing":
            await update.message.reply_text(
                "✍️ Writing exercise waiting!\n"
                "Write in German, or /skip to move to vocab quiz.")
        elif phase == "vocab_quiz":
            await update.message.reply_text(
                "📝 Vocab quiz in progress! Answer the question above.")
        else:
            await update.message.reply_text(
                "📝 Active lesson! Finish the quiz or /skip.")
        return
    await do_lesson(ctx.bot, update.effective_chat.id, uid)


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = uget(uid)
    phase = u.get("phase")

    if phase == "writing":
        # Skip writing → go to vocab quiz
        await update.message.reply_text("⏭️ Writing skipped — let's test your vocab!")
        await start_vocab_quiz(ctx.bot, update.effective_chat.id, uid)
    elif phase == "vocab_quiz":
        # Skip vocab quiz → finish lesson
        await update.message.reply_text("⏭️ Vocab quiz skipped.")
        await finish_lesson(ctx.bot, update.effective_chat.id, uid)
    else:
        # Skip entire lesson
        uset(uid, lesson=None, phase=None, q_idx=0,
             score_l=0, score_r=0, v_idx=0, score_v=0, wrote=False)
        await update.message.reply_text("⏭️ Lesson skipped. Use /lesson to start a new one.")


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

    # Listening/Reading quiz answers
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

        uset(uid, q_idx=q_idx + 1)
        chat_id = query.message.chat_id
        await send_q(ctx.bot, chat_id, uid)
        return

    # Vocab quiz answers
    if data.startswith("vq_"):
        parts = data.split("_")
        v_idx = int(parts[1])
        chosen = int(parts[2])

        u = uget(uid)
        lesson = u.get("lesson")
        if not lesson or "vocab_quiz" not in lesson:
            await query.edit_message_text("⚠️ Session expired. Use /lesson to start new.")
            return

        vq = lesson["vocab_quiz"]
        if v_idx >= len(vq):
            return

        q = vq[v_idx]
        correct = chosen == q["ans"]
        if correct:
            uset(uid, score_v=u.get("score_v", 0) + 1)

        emoji = "✅" if correct else "❌"
        correct_opt = q["opts"][q["ans"]]

        await query.edit_message_text(
            f"📝 <b>{q['de']}</b>\n\n"
            f"{emoji} {'Correct!' if correct else f'Answer: <b>{correct_opt}</b>'}",
            parse_mode="HTML")

        uset(uid, v_idx=v_idx + 1)
        chat_id = query.message.chat_id
        await send_vq(ctx.bot, chat_id, uid)


# ═══════════════════════════
#  CHAT MODE — free practice
# ═══════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = uget(uid)
    user_text = update.message.text
    if not user_text:
        return

    # Writing exercise phase
    if u.get("phase") == "writing" and u.get("lesson"):
        lesson = u["lesson"]
        await update.message.reply_text("📝 Checking your writing...")
        try:
            feedback = await check_writing(user_text, lesson["topic"], u["level"])
            await update.message.reply_text(feedback)
        except Exception as e:
            log.error(f"Writing check error: {e}")
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                await update.message.reply_text(
                    "⏳ API limit reached. Try again in 10-15 minutes.")
            else:
                await update.message.reply_text("❌ Could not check your writing. Try again!")
            return

        uset(uid, wrote=True)
        await update.message.reply_text(
            "✅ Great job! Now let's test your vocabulary...",
            parse_mode="HTML")
        await start_vocab_quiz(ctx.bot, update.effective_chat.id, uid)
        return

    # If in quiz phase, remind to finish
    if u.get("lesson") and u.get("phase") in ("listening", "reading", "vocab_quiz"):
        await update.message.reply_text(
            "📝 Active quiz! Answer the question above.\n"
            "Or use /skip to skip.")
        return

    # Regular chat mode
    try:
        history = u.get("chat", [])
        reply = await chat_reply(user_text, u["level"], history)

        history.append({"role": "user", "text": user_text})
        history.append({"role": "model", "text": reply})
        uset(uid, chat=history[-20:])

        await update.message.reply_text(reply)
    except Exception as e:
        log.error(f"Chat error: {e}")
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            await update.message.reply_text(
                "⏳ API limit reached (free tier). Try again in 10-15 minutes.")
        else:
            await update.message.reply_text("❌ Something went wrong. Try again!")


# ═══════════════════════════
#  SCHEDULER — daily lessons
# ═══════════════════════════
def schedule_user(app, uid, hour, minute):
    job_id = f"daily_{uid}"
    existing = app.job_queue.get_jobs_by_name(job_id)
    for job in existing:
        job.schedule_removal()
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
        uset(uid, lesson=None, phase=None, q_idx=0,
             score_l=0, score_r=0, v_idx=0, score_v=0, wrote=False)
    await do_lesson(ctx.bot, chat_id, uid)


async def post_init(app: Application):
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
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("level", cmd_level))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def error_handler(update, ctx):
        log.error(f"Unhandled error: {ctx.error}", exc_info=ctx.error)

    app.add_error_handler(error_handler)

    log.info(f"Bot starting... data path: {DATA}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
