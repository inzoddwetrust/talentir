#!/bin/bash

INSTALL_PATH="/opt/jetup"
SERVICE_NAME="jetup-bot"
GITHUB_REPO="git@github.com:inzoddwetrust/jetup.git"

# Функция логирования
log() {
   echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

# Функция обработки ошибок
handle_error() {
   log "Error occurred in script at line: ${1}"
   exit 1
}

trap 'handle_error ${LINENO}' ERR

# Проверка root прав
if [ "$EUID" -ne 0 ]; then
   log "Please run as root (with sudo)"
   exit 1
fi

# Прекращаем выполнение скрипта при любой ошибке
set -e

# Проверка и установка необходимых утилит
command -v ssh >/dev/null 2>&1 || {
   log "SSH is required but not installed. Installing..."
   apt-get install -y openssh-client
}

# Обновление системы
log "Updating system..."
apt-get update
apt-get upgrade -y

log "Installing sudo..."
apt-get install -y sudo

# Установка системных зависимостей
log "Installing system dependencies..."
apt-get install -y \
   build-essential \
   python3 \
   python3-venv \
   python3-dev \
   python3-pip \
   git \
   wkhtmltopdf \
   libssl-dev \
   zlib1g-dev \
   libbz2-dev \
   libreadline-dev \
   libsqlite3-dev \
   wget \
   curl \
   llvm \
   libncurses5-dev \
   libncursesw5-dev \
   xz-utils \
   tk-dev \
   libffi-dev \
   liblzma-dev

# Проверка наличия SSH ключа
if [ ! -f ~/.ssh/id_ed25519 ]; then
   log "SSH key not found. Generating..."
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519

   echo "Please add this public key to your GitHub repository deploy keys:"
   echo "------------------------"
   cat ~/.ssh/id_ed25519.pub
   echo "------------------------"

   echo "After adding the key to GitHub, press Enter to continue..."
   read -r
fi

# Проверка подключения к GitHub
if ! ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
   log "Error: Cannot authenticate with GitHub"
   log "Please ensure the SSH key is added to GitHub deploy keys"
   exit 1
fi

# Определение пользователя
if [ -n "$SUDO_USER" ]; then
   INSTALL_USER="$SUDO_USER"
else
   INSTALL_USER="$USER"
fi

# Проверка существующей установки
if [ -d "$INSTALL_PATH" ]; then
   log "Found existing installation at $INSTALL_PATH"

   if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
       log "Stopping existing service..."
       systemctl stop "$SERVICE_NAME"
       systemctl disable "$SERVICE_NAME"
   fi

   if [ -d "$INSTALL_PATH/bot" ]; then
       log "Creating backup of existing bot..."
       backup_dir="$INSTALL_PATH/backup_$(date +'%Y%m%d_%H%M%S')"
       mv "$INSTALL_PATH/bot" "$backup_dir"
       log "Backup created at $backup_dir"
   fi

   if [ -d "$INSTALL_PATH/venv" ]; then
       log "Removing existing virtual environment..."
       rm -rf "$INSTALL_PATH/venv"
   fi
else
   mkdir -p "$INSTALL_PATH"
fi

chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH"

# Клонирование репозитория
log "Cloning repository..."
su - "$INSTALL_USER" -c "git clone $GITHUB_REPO $INSTALL_PATH/bot" || {
   log "Failed to clone repository"
   exit 1
}

# Создание виртуального окружения
log "Creating virtual environment..."
python3 -m venv "$INSTALL_PATH/venv"
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH/venv"

# Активация виртуального окружения и установка пакетов
su - "$INSTALL_USER" -c "
   source $INSTALL_PATH/venv/bin/activate && \
   cd $INSTALL_PATH/bot && \
   pip install --upgrade pip && \
   pip install -r requirements.txt
"

# Создание конфигурационных файлов и директорий
log "Creating configuration files and directories..."
su - "$INSTALL_USER" -c "
   cd $INSTALL_PATH/bot && \
   cp .env.example .env && \
   mkdir -p temp doc_temp
"

# Создание systemd сервиса
log "Creating systemd service..."
tee /etc/systemd/system/"$SERVICE_NAME".service << EOF
[Unit]
Description=Jetup Investment Bot
After=network.target

[Service]
Type=simple
User=$INSTALL_USER
WorkingDirectory=$INSTALL_PATH/bot
Environment="PATH=$INSTALL_PATH/venv/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Перезагрузка systemd
log "Reloading systemd..."
systemctl daemon-reload

log "Setup complete! Please:"
log "1. Edit .env file in $INSTALL_PATH/bot/"
log "2. Place google_credentials.json in $INSTALL_PATH/bot/"
log "3. Run these commands when configuration is ready:"
log "   sudo systemctl enable $SERVICE_NAME"
log "   sudo systemctl start $SERVICE_NAME"
log "4. Check status with: sudo systemctl status $SERVICE_NAME"