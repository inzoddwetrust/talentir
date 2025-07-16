#!/bin/bash

INSTALL_PATH="/opt/talentir"
SERVICE_NAME="talentir-bot"
GITHUB_REPO="git@github.com-talentir:inzoddwetrust/talentir.git"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Функция логирования
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING: $1${NC}"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1${NC}"
}

info() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] INFO: $1${NC}"
}

# Функция обработки ошибок
handle_error() {
    error "Error occurred in script at line: ${1}"
    exit 1
}

trap 'handle_error ${LINENO}' ERR

# КРИТИЧЕСКАЯ ПРОВЕРКА - НЕ ТРОГАЕМ JETUP!
if [[ "$INSTALL_PATH" == "/opt/jetup" ]]; then
    error "INSTALL_PATH is set to /opt/jetup! This would overwrite JetUp installation!"
    error "This script is for Talentir installation only!"
    exit 1
fi

# Проверка root прав
if [ "$EUID" -ne 0 ]; then
    error "Please run as root (with sudo)"
    exit 1
fi

# Показываем что будем делать
info "This script will install Talentir bot to: $INSTALL_PATH"
info "Service name: $SERVICE_NAME"
info "Repository: $GITHUB_REPO"
echo ""
warn "This will NOT affect your JetUp installation at /opt/jetup"
echo ""
read -p "Continue with installation? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    log "Installation cancelled"
    exit 0
fi

# Прекращаем выполнение скрипта при любой ошибке
set -e

# Проверка и установка необходимых утилит
command -v ssh >/dev/null 2>&1 || {
    log "SSH is required but not installed. Installing..."
    apt-get install -y openssh-client
}

# Обновление системы
log "Updating system packages..."
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
    warn "SSH key not found. Generating new key..."
    ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519

    echo ""
    info "Please add this public key to your GitHub repository deploy keys:"
    info "Go to: https://github.com/inzoddwetrust/talentir/settings/keys"
    echo "------------------------"
    cat ~/.ssh/id_ed25519.pub
    echo "------------------------"
    echo ""
    read -p "After adding the key to GitHub, press Enter to continue..."
else
    log "SSH key found, using existing key"
fi

# Проверка подключения к GitHub
log "Checking GitHub connection..."
if ! ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    error "Cannot authenticate with GitHub"
    error "Please ensure the SSH key is added to GitHub deploy keys"
    info "Your public key is:"
    cat ~/.ssh/id_ed25519.pub
    exit 1
fi

# Определение пользователя
if [ -n "$SUDO_USER" ]; then
    INSTALL_USER="$SUDO_USER"
else
    INSTALL_USER="$USER"
fi

# Проверка существующей установки Talentir
if [ -d "$INSTALL_PATH" ]; then
    warn "Found existing installation at $INSTALL_PATH"

    # Создаем полный бэкап существующей установки
    backup_name="talentir_backup_$(date +'%Y%m%d_%H%M%S')"
    backup_dir="/root/$backup_name"

    log "Creating FULL backup to $backup_dir"
    cp -r "$INSTALL_PATH" "$backup_dir"

    # Особо важно - сохраняем .env отдельно
    if [ -f "$INSTALL_PATH/bot/.env" ]; then
        cp "$INSTALL_PATH/bot/.env" "/root/$backup_name.env"
        info "Backed up .env to /root/$backup_name.env"
    fi

    if [ -f "$INSTALL_PATH/bot/google_credentials.json" ]; then
        cp "$INSTALL_PATH/bot/google_credentials.json" "/root/$backup_name.google_credentials.json"
        info "Backed up google_credentials.json"
    fi

    if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
        log "Stopping existing service..."
        systemctl stop "$SERVICE_NAME" || true
        systemctl disable "$SERVICE_NAME" || true
    fi

    # Удаляем старую установку
    log "Removing old installation..."
    rm -rf "$INSTALL_PATH"
fi

# Создаем директорию установки
mkdir -p "$INSTALL_PATH"
chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH"

# Клонирование репозитория
log "Cloning repository..."
su - "$INSTALL_USER" -c "git clone $GITHUB_REPO $INSTALL_PATH/bot" || {
    error "Failed to clone repository"
    error "Make sure:"
    error "1. The repository exists at $GITHUB_REPO"
    error "2. Your SSH key has access to it"
    exit 1
}

# Создание виртуального окружения
log "Creating virtual environment..."
python3 -m venv "$INSTALL_PATH/venv"
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH/venv"

# Активация виртуального окружения и установка пакетов
log "Installing Python dependencies..."
su - "$INSTALL_USER" -c "
    source $INSTALL_PATH/venv/bin/activate && \
    cd $INSTALL_PATH/bot && \
    pip install --upgrade pip && \
    pip install -r requirements.txt
"

# Создание конфигурационных файлов и директорий
log "Creating configuration structure..."
su - "$INSTALL_USER" -c "
    cd $INSTALL_PATH/bot && \
    mkdir -p temp doc_temp
"

# ВАЖНО: НЕ перезаписываем .env если он существует!
if [ ! -f "$INSTALL_PATH/bot/.env" ]; then
    if [ -f "$INSTALL_PATH/bot/.env.example" ]; then
        log "Creating .env from .env.example"
        su - "$INSTALL_USER" -c "cp $INSTALL_PATH/bot/.env.example $INSTALL_PATH/bot/.env"
    else
        warn ".env.example not found, creating empty .env"
        su - "$INSTALL_USER" -c "touch $INSTALL_PATH/bot/.env"
    fi
else
    info ".env already exists, keeping existing file"
fi

# Создание systemd сервиса
log "Creating systemd service..."
tee /etc/systemd/system/"$SERVICE_NAME".service << EOF
[Unit]
Description=Talentir Investment Bot
After=network.target

[Service]
Type=simple
User=$INSTALL_USER
WorkingDirectory=$INSTALL_PATH/bot
Environment="PATH=$INSTALL_PATH/venv/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 main.py
Restart=always
RestartSec=10

# Логирование
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Перезагрузка systemd
log "Reloading systemd..."
systemctl daemon-reload

echo ""
log "Installation completed successfully!"
echo ""
info "Installation path: $INSTALL_PATH"
info "Service name: $SERVICE_NAME"
echo ""
warn "IMPORTANT NEXT STEPS:"
echo "1. Edit configuration file:"
echo "   nano $INSTALL_PATH/bot/.env"
echo ""
echo "2. Add your settings:"
echo "   - TELEGRAM_API_TOKEN"
echo "   - GOOGLE_SHEET_ID"
echo "   - POSTMARK_API_TOKEN"
echo "   - Database and other settings"
echo ""
echo "3. Place google_credentials.json:"
echo "   cp /path/to/google_credentials.json $INSTALL_PATH/bot/"
echo ""
echo "4. When ready, start the bot:"
echo "   sudo systemctl enable $SERVICE_NAME"
echo "   sudo systemctl start $SERVICE_NAME"
echo ""
echo "5. Check status:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "   sudo journalctl -u $SERVICE_NAME -f"
echo ""
info "Your JetUp installation at /opt/jetup was NOT touched!"