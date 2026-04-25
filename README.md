# DeutschDaily

A Telegram bot that sends you a daily German lesson with listening, reading, writing exercises, vocabulary quizzes, and free chat practice — all powered by AI.

## Features

**Daily Lessons** — Auto-generated lessons on diverse topics (science, history, culture, tech, psychology, travel, and more)

Each lesson includes:
1. **Listening** — TTS audio of a German text + 3 comprehension questions
2. **Reading** — Read the text + 3 True/False/Not Given questions
3. **Speaking** — Key phrases with audio to listen and repeat
4. **Writing** — Write your own text, AI checks grammar and gives corrections
5. **Vocab Quiz** — 7 new words tested with inline keyboard (German → English)

**Free Chat** — Write in German anytime, get corrections and tips from an AI tutor

**Progress Tracking** — `/progress` shows your score history, streaks, and averages

## Tech Stack

- **Python 3.11** + `python-telegram-bot` v21.3
- **Google Gemini API** (gemini-2.5-flash-lite) for lesson generation, writing feedback, and chat
- **edge-tts** for German text-to-speech audio
- **Railway** for 24/7 cloud hosting with persistent volume

## Commands

| Command | Description |
|---------|-------------|
| `/lesson` | Get a lesson now |
| `/momente` | Set Momente A2.1 textbook unit |
| `/progress` | View your learning stats |
| `/level` | Set your German level (A1–B1.2) |
| `/time` | Set daily lesson time (Budapest TZ) |
| `/streak` | View your streak |
| `/skip` | Skip current exercise |
| `/help` | Show all commands |

## Setup

### Environment Variables

```
BOT_TOKEN=your_telegram_bot_token
GEMINI_KEY=your_google_gemini_api_key
```

### Local Development

```bash
pip install -r requirements.txt
export BOT_TOKEN=... GEMINI_KEY=...
python bot.py
```

### Deploy to Railway

```bash
railway login
railway init
railway volume add --mount-path /data
railway variables set BOT_TOKEN=... GEMINI_KEY=...
railway up
```

## Lesson Flow

```
/lesson
  → Audio (TTS) + 3 Listening Questions
  → Text + 3 Reading Questions (T/F/NG)
  → Quiz Results + Speaking Phrases + Vocab List
  → Writing Exercise → AI Feedback
  → Vocab Quiz (7 words, inline keyboards)
  → Final Score + Progress Saved
```

## License

MIT
