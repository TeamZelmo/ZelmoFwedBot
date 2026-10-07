████████╗ ██████╗       ███████╗██╗    ██╗██████╗ 
╚══██╔══╝██╔════╝       ██╔════╝██║    ██║██╔══██╗
   ██║   ██║  ███╗█████╗█████╗  ██║ █╗ ██║██║  ██║
   ██║   ██║   ██║╚════╝██╔══╝  ██║███╗██║██║  ██║
   ██║   ╚██████╔╝      ██║     ╚███╔███╔╝██████╔╝
   ╚═╝    ╚═════╝       ╚═╝      ╚══╝╚══╝ ╚═════╝ 
⚡ TG-FORWARDER PROProduction-Ready, High-Concurrency Telegram Batch Migration Bot✨ Features •
⚙️ Configuration •
📋 Commands •
🚀 Deployment •
🏗️ Architecture •
🛡️ Error Handling✨ FeaturesHar Tarah Ka Media Support: Text messages, Full-HD Photos, Streamable Videos, Documents (bina size/mime restriction), Audio tracks, Voice Notes, Round Video Notes, Stickers (Static, TGS Animated, WEBM Video), Polls, Contacts aur Locations.Batch Range Execution: Single command syntax se continuous ranges copy karein (start_id-end_id).Zero Freeze Concurrency: Har user ke liye isolated async sessions, jisse ek user ka task dusre user ko slow nahi karta.FloodWait Auto-Shield: Telegram ke exact mandate kiye gaye rate-limit seconds detect karke bot gracefully auto-sleep karta hai.Exponential Backoff: Transient network aur RPC errors par automatic $2^n$ second backoff retry mechanism.Live Progress & ETA: Percentage, processed count aur approximate time left ka real-time visual progress counter.Access Lockdown: OWNER_ID configure karke bot ko private ya multi-user public mode me run karne ka option.⚙️ ConfigurationProject root me ek .env file banayein aur required variables add karein:# =================================================================
# 🔑 TELEGRAM API CREDENTIALS (MANDATORY)
# =================================================================
API_ID=12345678                                     # https://my.telegram.org se lein
API_HASH=abcdef1234567890abcdef1234567890         # https://my.telegram.org se lein
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # @BotFather se generate karein

# =================================================================
# ⚙️ BOT ENGINE SETTINGS (OPTIONAL)
# =================================================================
OWNER_ID=0          # 0 = Sabhi users ke liye open | Telegram numeric ID = Admin locked
MSG_DELAY=0.8       # Messages ke beech delay in seconds (flood-ban se bachne ke liye)
MAX_RETRY=3         # RPC failures ke dauran maximum retry attempts
📋 Commands & UsageCommandFormatAccessKaam/start/startPublicBot status, syntax aur guidance menu dikhata hai/batch/batch <link_range> <dest_id>AuthorizedTarget range ka copying task shuru karta hai/status/statusAuthorizedRunning batch ka current progress percentage aur ETA deta hai/cancel/cancelAuthorizedActive chal rahe task ko turant interrupt/stop karta hai💡 Syntax Examples# 1. Public Channel se destination channel me forward karna:
/batch https://t.me/PublicChannel/100-250 -1001987654321

# 2. Private Channel (Numeric ID via t.me/c/...) se copy karna:
/batch https://t.me/c/1837492819/500-750 -1001234567890

# 3. Apne direct "Saved Messages" me backup banana:
/batch https://t.me/SourceChannel/1-100 me
Important: Private channels ke messages copy karne ke liye bot (ya configured user session) ka source aur destination dono channels me hona zaroori hai.🚀 Deployment GuideOption 1: VPS / Ubuntu Server# 1. System packages update karein
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Repository clone aur enter karein
git clone https://github.com/your-username/tg-forwarder.git
cd tg-forwarder

# 3. Python virtual environment banayein aur activate karein
python3 -m venv venv
source venv/bin/activate

# 4. Dependencies install karein
pip install -r requirements.txt

# 5. Environment config setup karein
cp .env.example .env
nano .env

# 6. Background screen me bot run karein
screen -S forwarder
python forwarder_bot.py
# Detach karne ke liye: Press Ctrl+A then D
Systemd Background Service (Production Best Practice)sudo nano /etc/systemd/system/tgforwarder.service
Paste configuration:[Unit]
Description=Telegram Forwarder Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-forwarder
ExecStart=/root/tg-forwarder/venv/bin/python forwarder_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-forwarder/.env

[Install]
WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now tgforwarder
sudo systemctl status tgforwarder
Option 2: Docker & Docker Compose# docker-compose.yml
version: "3.9"
services:
  tg-forwarder:
    build: .
    container_name: tg_forwarder_app
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
Container start karein:docker compose up -d --build
docker compose logs -f
🏗️ System Architecture┌──────────────────────────────────────────────────────────┐
│                   TELEGRAM USER (CLIENT)                 │
│                 /batch   /status   /cancel               │
└────────────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Pyrogram Client │
                    └────────┬────────┘
                             │
               ┌─────────────▼──────────────┐
               │    Per-User Session Pool   │
               │   (Isolated Dataclasses)   │
               └───────┬──────────────┬─────┘
                       │              │
        User A Task    │              │    User B Task
        ┌──────────────▼──────┐   ┌───▼─────────────────┐
        │ asyncio.create_task │   │ asyncio.create_task │
        └──────────────┬──────┘   └───┬─────────────────┘
                       │              │
            ┌──────────▼──────────────▼──────────┐
            │       Execution & Dispatcher       │
            │  • FloodWait Catcher (sleep + 1s)  │
            │  • RPC Exponential Backoff (2^n)   │
            │  • Deleted Message ID Bypass       │
            └──────────────────┬─────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Destination Target │
                    └─────────────────────┘
🛡️ Error HandlingExceptionLevelBehaviorFloodWaitWarningMandated seconds tak task automatically pause hota hai aur timer finish hote hi resume ho jata hai.MessageIdInvalidInfoChannel me deleted ya empty IDs ko silently skip karke agle message par move karta hai.ChatWriteForbiddenCriticalDestination channel me posting rights na hone par process safe-stop ho kar user ko notify karti hai.RPCErrorTransientMax retry limit tak automatic delay retry lagata hai bina crash hue.📁 Repository Treetg-forwarder/
├── forwarder_bot.py       # Core bot logic & Pyrogram routing
├── requirements.txt       # Dependencies (Pyrogram, TgCrypto, python-dotenv)
├── Dockerfile             # Container definition file
├── docker-compose.yml     # Multi-container orchestration config
├── .env.example           # Environment template
└── README.md              # Project documentation
📜 LicenseYeh project MIT License ke tahat release kiya gaya hai.Disclaimer: Yeh tool strictly personal backup aur authorized message migration ke liye banaya gaya hai. Telegram Terms of Service aur copyright laws ka palan karna user ki responsibility hai.████████╗ ██████╗       ███████╗██╗    ██╗██████╗ 
╚══██╔══╝██╔════╝       ██╔════╝██║    ██║██╔══██╗
   ██║   ██║  ███╗█████╗█████╗  ██║ █╗ ██║██║  ██║
   ██║   ██║   ██║╚════╝██╔══╝  ██║███╗██║██║  ██║
   ██║   ╚██████╔╝      ██║     ╚███╔███╔╝██████╔╝
   ╚═╝    ╚═════╝       ╚═╝      ╚══╝╚══╝ ╚═════╝ 
⚡ TG-FORWARDER PROProduction-Ready, High-Concurrency Telegram Batch Migration Bot✨ Features •
⚙️ Configuration •
📋 Commands •
🚀 Deployment •
🏗️ Architecture •
🛡️ Error Handling✨ FeaturesHar Tarah Ka Media Support: Text messages, Full-HD Photos, Streamable Videos, Documents (bina size/mime restriction), Audio tracks, Voice Notes, Round Video Notes, Stickers (Static, TGS Animated, WEBM Video), Polls, Contacts aur Locations.Batch Range Execution: Single command syntax se continuous ranges copy karein (start_id-end_id).Zero Freeze Concurrency: Har user ke liye isolated async sessions, jisse ek user ka task dusre user ko slow nahi karta.FloodWait Auto-Shield: Telegram ke exact mandate kiye gaye rate-limit seconds detect karke bot gracefully auto-sleep karta hai.Exponential Backoff: Transient network aur RPC errors par automatic $2^n$ second backoff retry mechanism.Live Progress & ETA: Percentage, processed count aur approximate time left ka real-time visual progress counter.Access Lockdown: OWNER_ID configure karke bot ko private ya multi-user public mode me run karne ka option.⚙️ ConfigurationProject root me ek .env file banayein aur required variables add karein:# =================================================================
# 🔑 TELEGRAM API CREDENTIALS (MANDATORY)
# =================================================================
API_ID=12345678                                     # https://my.telegram.org se lein
API_HASH=abcdef1234567890abcdef1234567890         # https://my.telegram.org se lein
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # @BotFather se generate karein

# =================================================================
# ⚙️ BOT ENGINE SETTINGS (OPTIONAL)
# =================================================================
OWNER_ID=0          # 0 = Sabhi users ke liye open | Telegram numeric ID = Admin locked
MSG_DELAY=0.8       # Messages ke beech delay in seconds (flood-ban se bachne ke liye)
MAX_RETRY=3         # RPC failures ke dauran maximum retry attempts
📋 Commands & UsageCommandFormatAccessKaam/start/startPublicBot status, syntax aur guidance menu dikhata hai/batch/batch <link_range> <dest_id>AuthorizedTarget range ka copying task shuru karta hai/status/statusAuthorizedRunning batch ka current progress percentage aur ETA deta hai/cancel/cancelAuthorizedActive chal rahe task ko turant interrupt/stop karta hai💡 Syntax Examples# 1. Public Channel se destination channel me forward karna:
/batch https://t.me/PublicChannel/100-250 -1001987654321

# 2. Private Channel (Numeric ID via t.me/c/...) se copy karna:
/batch https://t.me/c/1837492819/500-750 -1001234567890

# 3. Apne direct "Saved Messages" me backup banana:
/batch https://t.me/SourceChannel/1-100 me
Important: Private channels ke messages copy karne ke liye bot (ya configured user session) ka source aur destination dono channels me hona zaroori hai.🚀 Deployment GuideOption 1: VPS / Ubuntu Server# 1. System packages update karein
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Repository clone aur enter karein
git clone https://github.com/your-username/tg-forwarder.git
cd tg-forwarder

# 3. Python virtual environment banayein aur activate karein
python3 -m venv venv
source venv/bin/activate

# 4. Dependencies install karein
pip install -r requirements.txt

# 5. Environment config setup karein
cp .env.example .env
nano .env

# 6. Background screen me bot run karein
screen -S forwarder
python forwarder_bot.py
# Detach karne ke liye: Press Ctrl+A then D
Systemd Background Service (Production Best Practice)sudo nano /etc/systemd/system/tgforwarder.service
Paste configuration:[Unit]
Description=Telegram Forwarder Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-forwarder
ExecStart=/root/tg-forwarder/venv/bin/python forwarder_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-forwarder/.env

[Install]
WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now tgforwarder
sudo systemctl status tgforwarder
Option 2: Docker & Docker Compose# docker-compose.yml
version: "3.9"
services:
  tg-forwarder:
    build: .
    container_name: tg_forwarder_app
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
Container start karein:docker compose up -d --build
docker compose logs -f
🏗️ System Architecture┌──────────────────────────────────────────────────────────┐
│                   TELEGRAM USER (CLIENT)                 │
│                 /batch   /status   /cancel               │
└────────────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Pyrogram Client │
                    └────────┬────────┘
                             │
               ┌─────────────▼──────────────┐
               │    Per-User Session Pool   │
               │   (Isolated Dataclasses)   │
               └───────┬──────────────┬─────┘
                       │              │
        User A Task    │              │    User B Task
        ┌──────────────▼──────┐   ┌───▼─────────────────┐
        │ asyncio.create_task │   │ asyncio.create_task │
        └──────────────┬──────┘   └───┬─────────────────┘
                       │              │
            ┌──────────▼──────────────▼──────────┐
            │       Execution & Dispatcher       │
            │  • FloodWait Catcher (sleep + 1s)  │
            │  • RPC Exponential Backoff (2^n)   │
            │  • Deleted Message ID Bypass       │
            └──────────────────┬─────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Destination Target │
                    └─────────────────────┘
🛡️ Error HandlingExceptionLevelBehaviorFloodWaitWarningMandated seconds tak task automatically pause hota hai aur timer finish hote hi resume ho jata hai.MessageIdInvalidInfoChannel me deleted ya empty IDs ko silently skip karke agle message par move karta hai.ChatWriteForbiddenCriticalDestination channel me posting rights na hone par process safe-stop ho kar user ko notify karti hai.RPCErrorTransientMax retry limit tak automatic delay retry lagata hai bina crash hue.📁 Repository Treetg-forwarder/
├── forwarder_bot.py       # Core bot logic & Pyrogram routing
├── requirements.txt       # Dependencies (Pyrogram, TgCrypto, python-dotenv)
├── Dockerfile             # Container definition file
├── docker-compose.yml     # Multi-container orchestration config
├── .env.example           # Environment template
└── README.md              # Project documentation
📜 LicenseYeh project MIT License ke tahat release kiya gaya hai.Disclaimer: Yeh tool strictly personal backup aur authorized message migration ke liye banaya gaya hai. Telegram Terms of Service aur copyright laws ka palan karna user ki responsibility hai.████████╗ ██████╗       ███████╗██╗    ██╗██████╗ 
╚══██╔══╝██╔════╝       ██╔════╝██║    ██║██╔══██╗
   ██║   ██║  ███╗█████╗█████╗  ██║ █╗ ██║██║  ██║
   ██║   ██║   ██║╚════╝██╔══╝  ██║███╗██║██║  ██║
   ██║   ╚██████╔╝      ██║     ╚███╔███╔╝██████╔╝
   ╚═╝    ╚═════╝       ╚═╝      ╚══╝╚══╝ ╚═════╝ 
⚡ TG-FORWARDER PROProduction-Ready, High-Concurrency Telegram Batch Migration Bot✨ Features •
⚙️ Configuration •
📋 Commands •
🚀 Deployment •
🏗️ Architecture •
🛡️ Error Handling✨ FeaturesHar Tarah Ka Media Support: Text messages, Full-HD Photos, Streamable Videos, Documents (bina size/mime restriction), Audio tracks, Voice Notes, Round Video Notes, Stickers (Static, TGS Animated, WEBM Video), Polls, Contacts aur Locations.Batch Range Execution: Single command syntax se continuous ranges copy karein (start_id-end_id).Zero Freeze Concurrency: Har user ke liye isolated async sessions, jisse ek user ka task dusre user ko slow nahi karta.FloodWait Auto-Shield: Telegram ke exact mandate kiye gaye rate-limit seconds detect karke bot gracefully auto-sleep karta hai.Exponential Backoff: Transient network aur RPC errors par automatic $2^n$ second backoff retry mechanism.Live Progress & ETA: Percentage, processed count aur approximate time left ka real-time visual progress counter.Access Lockdown: OWNER_ID configure karke bot ko private ya multi-user public mode me run karne ka option.⚙️ ConfigurationProject root me ek .env file banayein aur required variables add karein:# =================================================================
# 🔑 TELEGRAM API CREDENTIALS (MANDATORY)
# =================================================================
API_ID=12345678                                     # https://my.telegram.org se lein
API_HASH=abcdef1234567890abcdef1234567890         # https://my.telegram.org se lein
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # @BotFather se generate karein

# =================================================================
# ⚙️ BOT ENGINE SETTINGS (OPTIONAL)
# =================================================================
OWNER_ID=0          # 0 = Sabhi users ke liye open | Telegram numeric ID = Admin locked
MSG_DELAY=0.8       # Messages ke beech delay in seconds (flood-ban se bachne ke liye)
MAX_RETRY=3         # RPC failures ke dauran maximum retry attempts
📋 Commands & UsageCommandFormatAccessKaam/start/startPublicBot status, syntax aur guidance menu dikhata hai/batch/batch <link_range> <dest_id>AuthorizedTarget range ka copying task shuru karta hai/status/statusAuthorizedRunning batch ka current progress percentage aur ETA deta hai/cancel/cancelAuthorizedActive chal rahe task ko turant interrupt/stop karta hai💡 Syntax Examples# 1. Public Channel se destination channel me forward karna:
/batch https://t.me/PublicChannel/100-250 -1001987654321

# 2. Private Channel (Numeric ID via t.me/c/...) se copy karna:
/batch https://t.me/c/1837492819/500-750 -1001234567890

# 3. Apne direct "Saved Messages" me backup banana:
/batch https://t.me/SourceChannel/1-100 me
Important: Private channels ke messages copy karne ke liye bot (ya configured user session) ka source aur destination dono channels me hona zaroori hai.🚀 Deployment GuideOption 1: VPS / Ubuntu Server# 1. System packages update karein
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Repository clone aur enter karein
git clone https://github.com/your-username/tg-forwarder.git
cd tg-forwarder

# 3. Python virtual environment banayein aur activate karein
python3 -m venv venv
source venv/bin/activate

# 4. Dependencies install karein
pip install -r requirements.txt

# 5. Environment config setup karein
cp .env.example .env
nano .env

# 6. Background screen me bot run karein
screen -S forwarder
python forwarder_bot.py
# Detach karne ke liye: Press Ctrl+A then D
Systemd Background Service (Production Best Practice)sudo nano /etc/systemd/system/tgforwarder.service
Paste configuration:[Unit]
Description=Telegram Forwarder Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-forwarder
ExecStart=/root/tg-forwarder/venv/bin/python forwarder_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-forwarder/.env

[Install]
WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now tgforwarder
sudo systemctl status tgforwarder
Option 2: Docker & Docker Compose# docker-compose.yml
version: "3.9"
services:
  tg-forwarder:
    build: .
    container_name: tg_forwarder_app
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
Container start karein:docker compose up -d --build
docker compose logs -f
🏗️ System Architecture┌──────────────────────────────────────────────────────────┐
│                   TELEGRAM USER (CLIENT)                 │
│                 /batch   /status   /cancel               │
└────────────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Pyrogram Client │
                    └────────┬────────┘
                             │
               ┌─────────────▼──────────────┐
               │    Per-User Session Pool   │
               │   (Isolated Dataclasses)   │
               └───────┬──────────────┬─────┘
                       │              │
        User A Task    │              │    User B Task
        ┌──────────────▼──────┐   ┌───▼─────────────────┐
        │ asyncio.create_task │   │ asyncio.create_task │
        └──────────────┬──────┘   └───┬─────────────────┘
                       │              │
            ┌──────────▼──────────────▼──────────┐
            │       Execution & Dispatcher       │
            │  • FloodWait Catcher (sleep + 1s)  │
            │  • RPC Exponential Backoff (2^n)   │
            │  • Deleted Message ID Bypass       │
            └──────────────────┬─────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Destination Target │
                    └─────────────────────┘
🛡️ Error HandlingExceptionLevelBehaviorFloodWaitWarningMandated seconds tak task automatically pause hota hai aur timer finish hote hi resume ho jata hai.MessageIdInvalidInfoChannel me deleted ya empty IDs ko silently skip karke agle message par move karta hai.ChatWriteForbiddenCriticalDestination channel me posting rights na hone par process safe-stop ho kar user ko notify karti hai.RPCErrorTransientMax retry limit tak automatic delay retry lagata hai bina crash hue.📁 Repository Treetg-forwarder/
├── forwarder_bot.py       # Core bot logic & Pyrogram routing
├── requirements.txt       # Dependencies (Pyrogram, TgCrypto, python-dotenv)
├── Dockerfile             # Container definition file
├── docker-compose.yml     # Multi-container orchestration config
├── .env.example           # Environment template
└── README.md              # Project documentation
📜 LicenseYeh project MIT License ke tahat release kiya gaya hai.Disclaimer: Yeh tool strictly personal backup aur authorized message migration ke liye banaya gaya hai. Telegram Terms of Service aur copyright laws ka palan karna user ki responsibility hai.████████╗ ██████╗       ███████╗██╗    ██╗██████╗ 
╚══██╔══╝██╔════╝       ██╔════╝██║    ██║██╔══██╗
   ██║   ██║  ███╗█████╗█████╗  ██║ █╗ ██║██║  ██║
   ██║   ██║   ██║╚════╝██╔══╝  ██║███╗██║██║  ██║
   ██║   ╚██████╔╝      ██║     ╚███╔███╔╝██████╔╝
   ╚═╝    ╚═════╝       ╚═╝      ╚══╝╚══╝ ╚═════╝ 
⚡ TG-FORWARDER PROProduction-Ready, High-Concurrency Telegram Batch Migration Bot✨ Features •
⚙️ Configuration •
📋 Commands •
🚀 Deployment •
🏗️ Architecture •
🛡️ Error Handling✨ FeaturesHar Tarah Ka Media Support: Text messages, Full-HD Photos, Streamable Videos, Documents (bina size/mime restriction), Audio tracks, Voice Notes, Round Video Notes, Stickers (Static, TGS Animated, WEBM Video), Polls, Contacts aur Locations.Batch Range Execution: Single command syntax se continuous ranges copy karein (start_id-end_id).Zero Freeze Concurrency: Har user ke liye isolated async sessions, jisse ek user ka task dusre user ko slow nahi karta.FloodWait Auto-Shield: Telegram ke exact mandate kiye gaye rate-limit seconds detect karke bot gracefully auto-sleep karta hai.Exponential Backoff: Transient network aur RPC errors par automatic $2^n$ second backoff retry mechanism.Live Progress & ETA: Percentage, processed count aur approximate time left ka real-time visual progress counter.Access Lockdown: OWNER_ID configure karke bot ko private ya multi-user public mode me run karne ka option.⚙️ ConfigurationProject root me ek .env file banayein aur required variables add karein:# =================================================================
# 🔑 TELEGRAM API CREDENTIALS (MANDATORY)
# =================================================================
API_ID=12345678                                     # https://my.telegram.org se lein
API_HASH=abcdef1234567890abcdef1234567890         # https://my.telegram.org se lein
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # @BotFather se generate karein

# =================================================================
# ⚙️ BOT ENGINE SETTINGS (OPTIONAL)
# =================================================================
OWNER_ID=0          # 0 = Sabhi users ke liye open | Telegram numeric ID = Admin locked
MSG_DELAY=0.8       # Messages ke beech delay in seconds (flood-ban se bachne ke liye)
MAX_RETRY=3         # RPC failures ke dauran maximum retry attempts
📋 Commands & UsageCommandFormatAccessKaam/start/startPublicBot status, syntax aur guidance menu dikhata hai/batch/batch <link_range> <dest_id>AuthorizedTarget range ka copying task shuru karta hai/status/statusAuthorizedRunning batch ka current progress percentage aur ETA deta hai/cancel/cancelAuthorizedActive chal rahe task ko turant interrupt/stop karta hai💡 Syntax Examples# 1. Public Channel se destination channel me forward karna:
/batch https://t.me/PublicChannel/100-250 -1001987654321

# 2. Private Channel (Numeric ID via t.me/c/...) se copy karna:
/batch https://t.me/c/1837492819/500-750 -1001234567890

# 3. Apne direct "Saved Messages" me backup banana:
/batch https://t.me/SourceChannel/1-100 me
Important: Private channels ke messages copy karne ke liye bot (ya configured user session) ka source aur destination dono channels me hona zaroori hai.🚀 Deployment GuideOption 1: VPS / Ubuntu Server# 1. System packages update karein
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Repository clone aur enter karein
git clone https://github.com/your-username/tg-forwarder.git
cd tg-forwarder

# 3. Python virtual environment banayein aur activate karein
python3 -m venv venv
source venv/bin/activate

# 4. Dependencies install karein
pip install -r requirements.txt

# 5. Environment config setup karein
cp .env.example .env
nano .env

# 6. Background screen me bot run karein
screen -S forwarder
python forwarder_bot.py
# Detach karne ke liye: Press Ctrl+A then D
Systemd Background Service (Production Best Practice)sudo nano /etc/systemd/system/tgforwarder.service
Paste configuration:[Unit]
Description=Telegram Forwarder Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-forwarder
ExecStart=/root/tg-forwarder/venv/bin/python forwarder_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-forwarder/.env

[Install]
WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now tgforwarder
sudo systemctl status tgforwarder
Option 2: Docker & Docker Compose# docker-compose.yml
version: "3.9"
services:
  tg-forwarder:
    build: .
    container_name: tg_forwarder_app
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
Container start karein:docker compose up -d --build
docker compose logs -f
🏗️ System Architecture┌──────────────────────────────────────────────────────────┐
│                   TELEGRAM USER (CLIENT)                 │
│                 /batch   /status   /cancel               │
└────────────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Pyrogram Client │
                    └────────┬────────┘
                             │
               ┌─────────────▼──────────────┐
               │    Per-User Session Pool   │
               │   (Isolated Dataclasses)   │
               └───────┬──────────────┬─────┘
                       │              │
        User A Task    │              │    User B Task
        ┌──────────────▼──────┐   ┌───▼─────────────────┐
        │ asyncio.create_task │   │ asyncio.create_task │
        └──────────────┬──────┘   └───┬─────────────────┘
                       │              │
            ┌──────────▼──────────────▼──────────┐
            │       Execution & Dispatcher       │
            │  • FloodWait Catcher (sleep + 1s)  │
            │  • RPC Exponential Backoff (2^n)   │
            │  • Deleted Message ID Bypass       │
            └──────────────────┬─────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Destination Target │
                    └─────────────────────┘
🛡️ Error HandlingExceptionLevelBehaviorFloodWaitWarningMandated seconds tak task automatically pause hota hai aur timer finish hote hi resume ho jata hai.MessageIdInvalidInfoChannel me deleted ya empty IDs ko silently skip karke agle message par move karta hai.ChatWriteForbiddenCriticalDestination channel me posting rights na hone par process safe-stop ho kar user ko notify karti hai.RPCErrorTransientMax retry limit tak automatic delay retry lagata hai bina crash hue.📁 Repository Treetg-forwarder/
├── forwarder_bot.py       # Core bot logic & Pyrogram routing
├── requirements.txt       # Dependencies (Pyrogram, TgCrypto, python-dotenv)
├── Dockerfile             # Container definition file
├── docker-compose.yml     # Multi-container orchestration config
├── .env.example           # Environment template
└── README.md              # Project documentation
📜 LicenseYeh project MIT License ke tahat release kiya gaya hai.Disclaimer: Yeh tool strictly personal backup aur authorized message migration ke liye banaya gaya hai. Telegram Terms of Service aur copyright laws ka palan karna user ki responsibility hai.████████╗ ██████╗       ███████╗██╗    ██╗██████╗ 
╚══██╔══╝██╔════╝       ██╔════╝██║    ██║██╔══██╗
   ██║   ██║  ███╗█████╗█████╗  ██║ █╗ ██║██║  ██║
   ██║   ██║   ██║╚════╝██╔══╝  ██║███╗██║██║  ██║
   ██║   ╚██████╔╝      ██║     ╚███╔███╔╝██████╔╝
   ╚═╝    ╚═════╝       ╚═╝      ╚══╝╚══╝ ╚═════╝ 
⚡ TG-FORWARDER PROProduction-Ready, High-Concurrency Telegram Batch Migration Bot✨ Features •
⚙️ Configuration •
📋 Commands •
🚀 Deployment •
🏗️ Architecture •
🛡️ Error Handling✨ FeaturesHar Tarah Ka Media Support: Text messages, Full-HD Photos, Streamable Videos, Documents (bina size/mime restriction), Audio tracks, Voice Notes, Round Video Notes, Stickers (Static, TGS Animated, WEBM Video), Polls, Contacts aur Locations.Batch Range Execution: Single command syntax se continuous ranges copy karein (start_id-end_id).Zero Freeze Concurrency: Har user ke liye isolated async sessions, jisse ek user ka task dusre user ko slow nahi karta.FloodWait Auto-Shield: Telegram ke exact mandate kiye gaye rate-limit seconds detect karke bot gracefully auto-sleep karta hai.Exponential Backoff: Transient network aur RPC errors par automatic $2^n$ second backoff retry mechanism.Live Progress & ETA: Percentage, processed count aur approximate time left ka real-time visual progress counter.Access Lockdown: OWNER_ID configure karke bot ko private ya multi-user public mode me run karne ka option.⚙️ ConfigurationProject root me ek .env file banayein aur required variables add karein:# =================================================================
# 🔑 TELEGRAM API CREDENTIALS (MANDATORY)
# =================================================================
API_ID=12345678                                     # https://my.telegram.org se lein
API_HASH=abcdef1234567890abcdef1234567890         # https://my.telegram.org se lein
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # @BotFather se generate karein

# =================================================================
# ⚙️ BOT ENGINE SETTINGS (OPTIONAL)
# =================================================================
OWNER_ID=0          # 0 = Sabhi users ke liye open | Telegram numeric ID = Admin locked
MSG_DELAY=0.8       # Messages ke beech delay in seconds (flood-ban se bachne ke liye)
MAX_RETRY=3         # RPC failures ke dauran maximum retry attempts
📋 Commands & UsageCommandFormatAccessKaam/start/startPublicBot status, syntax aur guidance menu dikhata hai/batch/batch <link_range> <dest_id>AuthorizedTarget range ka copying task shuru karta hai/status/statusAuthorizedRunning batch ka current progress percentage aur ETA deta hai/cancel/cancelAuthorizedActive chal rahe task ko turant interrupt/stop karta hai💡 Syntax Examples# 1. Public Channel se destination channel me forward karna:
/batch https://t.me/PublicChannel/100-250 -1001987654321

# 2. Private Channel (Numeric ID via t.me/c/...) se copy karna:
/batch https://t.me/c/1837492819/500-750 -1001234567890

# 3. Apne direct "Saved Messages" me backup banana:
/batch https://t.me/SourceChannel/1-100 me
Important: Private channels ke messages copy karne ke liye bot (ya configured user session) ka source aur destination dono channels me hona zaroori hai.🚀 Deployment GuideOption 1: VPS / Ubuntu Server# 1. System packages update karein
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Repository clone aur enter karein
git clone https://github.com/your-username/tg-forwarder.git
cd tg-forwarder

# 3. Python virtual environment banayein aur activate karein
python3 -m venv venv
source venv/bin/activate

# 4. Dependencies install karein
pip install -r requirements.txt

# 5. Environment config setup karein
cp .env.example .env
nano .env

# 6. Background screen me bot run karein
screen -S forwarder
python forwarder_bot.py
# Detach karne ke liye: Press Ctrl+A then D
Systemd Background Service (Production Best Practice)sudo nano /etc/systemd/system/tgforwarder.service
Paste configuration:[Unit]
Description=Telegram Forwarder Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-forwarder
ExecStart=/root/tg-forwarder/venv/bin/python forwarder_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/root/tg-forwarder/.env

[Install]
WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now tgforwarder
sudo systemctl status tgforwarder
Option 2: Docker & Docker Compose# docker-compose.yml
version: "3.9"
services:
  tg-forwarder:
    build: .
    container_name: tg_forwarder_app
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
Container start karein:docker compose up -d --build
docker compose logs -f
🏗️ System Architecture┌──────────────────────────────────────────────────────────┐
│                   TELEGRAM USER (CLIENT)                 │
│                 /batch   /status   /cancel               │
└────────────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Pyrogram Client │
                    └────────┬────────┘
                             │
               ┌─────────────▼──────────────┐
               │    Per-User Session Pool   │
               │   (Isolated Dataclasses)   │
               └───────┬──────────────┬─────┘
                       │              │
        User A Task    │              │    User B Task
        ┌──────────────▼──────┐   ┌───▼─────────────────┐
        │ asyncio.create_task │   │ asyncio.create_task │
        └──────────────┬──────┘   └───┬─────────────────┘
                       │              │
            ┌──────────▼──────────────▼──────────┐
            │       Execution & Dispatcher       │
            │  • FloodWait Catcher (sleep + 1s)  │
            │  • RPC Exponential Backoff (2^n)   │
            │  • Deleted Message ID Bypass       │
            └──────────────────┬─────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Destination Target │
                    └─────────────────────┘
🛡️ Error HandlingExceptionLevelBehaviorFloodWaitWarningMandated seconds tak task automatically pause hota hai aur timer finish hote hi resume ho jata hai.MessageIdInvalidInfoChannel me deleted ya empty IDs ko silently skip karke agle message par move karta hai.ChatWriteForbiddenCriticalDestination channel me posting rights na hone par process safe-stop ho kar user ko notify karti hai.RPCErrorTransientMax retry limit tak automatic delay retry lagata hai bina crash hue.📁 Repository Treetg-forwarder/
├── forwarder_bot.py       # Core bot logic & Pyrogram routing
├── requirements.txt       # Dependencies (Pyrogram, TgCrypto, python-dotenv)
├── Dockerfile             # Container definition file
├── docker-compose.yml     # Multi-container orchestration config
├── .env.example           # Environment template
└── README.md              # Project documentation
📜 LicenseYeh project MIT License ke tahat release kiya gaya hai.Disclaimer: Yeh tool strictly personal backup aur authorized message migration ke liye banaya gaya hai. Telegram Terms of Service aur copyright laws ka palan karna user ki responsibility hai.
