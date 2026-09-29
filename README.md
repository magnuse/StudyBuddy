# StudyBuddy

A Telegram study coach for one family. Parents send the school's weekly plan, materials and Spanish word lists to the bot. The bot writes quiz questions in Swedish, parents approve them, and after school the student gets short batches of questions. Answers are graded with feedback in Swedish, and the parents get a weekly report.

The design guide lives at <https://claude.ai/artifact/5c75TEi9K6azjUp2JTHaoU>.

## How the week works

| Day | Afternoon (16:30): topic week | Evening (19:00): Spanish |
|---|---|---|
| Sunday | 18:00: bot asks parents for the week's plan | Words he still misses |
| Monday | Easy questions | Practice test on the current word list; reminder to send the new list |
| Tuesday | New parts and repeats | 07:15 warm-up before the word test; new list starts |
| Wednesday | Harder questions | Typed answers, both directions |
| Thursday | Practice test on the whole week | Words he missed |
| Friday | 07:15 warm-up; weekly report to parents | Short round |
| Saturday | Review of earlier topic weeks | Mixed round |

Public holidays, `/holiday` weeks and `/pause` stop all quizzes.

## Commands

Parents: `/status`, `/material`, `/words`, `/subjects`, `/schedule`, `/holiday`, `/pause`, `/resume`, `/invite parent|student`, `/family`, `/update`, `/version`, `/test_models`.
Student: `/quiz`, `/snooze`, `/progress`.

Parents add material by sending a PDF, Word file, photo or text to the bot. Put the subject and test date in the caption, for example `SO, prov fredag` or `Spanska glosor`. Without a caption, the bot guesses and asks you to confirm.

## Setup on Unraid

1. **Telegram bot.** Talk to `@BotFather`, send `/newbot`, and keep the token. Get your own user id from `@userinfobot`.
2. **Deploy key.** Run `ssh-keygen -t ed25519 -N "" -f /mnt/user/appdata/studybuddy/deploy_key`. In GitHub, open this repository's Settings → Deploy keys → Add, paste `deploy_key.pub`, and leave write access off.
3. **Base image access.** The first push to `main` builds `ghcr.io/magnuse/studybuddy-base`. Either make that package public in GitHub (it only contains Python and Tesseract), or run `docker login ghcr.io` on Unraid with a personal access token that has `read:packages`.
4. **Files.** In `/mnt/user/appdata/studybuddy/`, create `config.yaml` from `config.example.yaml`. Next to the compose file, create `.env` from `.env.example`.
5. **Anthropic API key** (for cloud grading). Create one at console.anthropic.com, add prepaid credit and set a monthly limit.
6. **Start.** Install the *Docker Compose Manager* plugin, add a stack with `docker-compose.yml`, and start it.
7. **Models.** Pull the local models: `docker exec -it <ollama container> ollama pull gemma3:12b` and `ollama pull gemma3:4b`. Try `/test_models` to compare speed and quality.
8. **Family.** Send `/start` to the bot. Then `/invite parent` for the other parent and `/invite student` for your son, and add subjects with `/subjects add Spanska`, `/subjects add SO` and so on.

## Updates

`entrypoint.sh` runs at every container start:

1. Pulls the `stable` branch with the deploy key.
2. Reinstalls Python packages only if `requirements.txt` changed.
3. Backs up the database (14 newest copies in `data/backups/`) and applies migrations at start.
4. Starts the bot. Once it is connected to Telegram, that commit is marked as good.

If a new version fails to start three times in a row, the entrypoint goes back to the last good commit and the parents get a Telegram message. The bot checks for new code every night at 03:30 and restarts itself if there is some and no quiz is running. `/update` does the same right away.

CI runs the tests on every push to `main`. Only when they pass is `main` pushed to `stable`, so the server never pulls untested code. Changes to `Dockerfile` or `entrypoint.sh` rebuild the base image; after that, click *Update* on the stack in Unraid.

## AI models

Each job has an ordered list of providers in `config.yaml` (`llm.tasks`). Question writing uses a local 12B model through Ollama, in the background. Grading uses Claude Opus 5 in the cloud, with the local 4B model as backup. Multiple choice and most vocabulary answers are checked in code. `allow_cloud: false` keeps everything at home. LM Studio or any OpenAI-compatible server works with `type: openai_compatible`.

Claude requests use the server-side refusal fallback (`fallbacks: "default"`), so a declined request is retried on another model instead of failing. Set `refusal_fallback: false` on the provider to turn it off.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
TELEGRAM_BOT_TOKEN=... FIRST_PARENT_CHAT_ID=... python -m app.main
```

Code layout: `app/main.py` wires everything, `handlers_parent.py` and `handlers_student.py` hold the Telegram flows, `scheduler.py` decides what to send when, `vocab.py` checks vocabulary answers, `generate.py` and `grade.py` hold the model prompts, `llm/` holds the providers and the router, and `db.py` holds the SQLite schema.
