#!/bin/bash

INSTALL_PATH="/opt/talentir"
SERVICE_NAME="talentir-bot"

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

handle_error() {
    error "Error occurred in script at line: ${1}"
    exit 1
}

trap 'handle_error ${LINENO}' ERR

# КРИТИЧЕСКАЯ ПРОВЕРКА
if [[ "$INSTALL_PATH" == "/opt/jetup" ]]; then
    error "INSTALL_PATH is set to /opt/jetup! This script is for Talentir only!"
    exit 1
fi

# Проверка root прав
if [ "$EUID" -ne 0 ]; then
    error "Please run as root (with sudo)"
    exit 1
fi

# Определение пользователя
if [ -n "$SUDO_USER" ]; then
    INSTALL_USER="$SUDO_USER"
else
    INSTALL_USER="$USER"
fi

# Проверка существования установки
if [ ! -d "$INSTALL_PATH/bot" ]; then
    error "Talentir installation not found at $INSTALL_PATH/bot"
    exit 1
fi

if [ ! -d "$INSTALL_PATH/venv" ]; then
    error "Virtual environment not found at $INSTALL_PATH/venv"
    exit 1
fi

# КРИТИЧЕСКИ ВАЖНО - сохраняем конфиги
info "Backing up configuration files..."
timestamp=$(date +'%Y%m%d_%H%M%S')
backup_dir="/root/talentir_update_backup_$timestamp"
mkdir -p "$backup_dir"

# Копируем важные файлы
if [ -f "$INSTALL_PATH/bot/.env" ]; then
    cp "$INSTALL_PATH/bot/.env" "$backup_dir/.env"
    info "Backed up .env"
fi

if [ -f "$INSTALL_PATH/bot/google_credentials.json" ]; then
    cp "$INSTALL_PATH/bot/google_credentials.json" "$backup_dir/google_credentials.json"
    info "Backed up google_credentials.json"
fi

if [ -f "$INSTALL_PATH/bot/talentir.db" ]; then
    cp "$INSTALL_PATH/bot/talentir.db" "$backup_dir/talentir.db"
    info "Backed up database"
fi

# Полный бэкап кода
log "Creating full code backup..."
cp -r "$INSTALL_PATH/bot" "$backup_dir/bot_full"
info "Full backup created at $backup_dir"

# Остановка сервиса
log "Stopping service..."
systemctl stop "$SERVICE_NAME"

# Сохраняем список локальных изменений
cd "$INSTALL_PATH/bot"
git status > "$backup_dir/git_status.txt"
git diff > "$backup_dir/git_diff.txt"

# Получение обновлений
log "Fetching updates from repository..."
su - "$INSTALL_USER" -c "
    cd $INSTALL_PATH/bot && \
    git fetch origin
" || {
    error "Failed to fetch updates"
    log "Starting service with old version..."
    systemctl start "$SERVICE_NAME"
    exit 1
}

# Проверка, есть ли обновления
if su - "$INSTALL_USER" -c "cd $INSTALL_PATH/bot && git diff HEAD origin/master --quiet"; then
    log "No updates available"
    systemctl start "$SERVICE_NAME"
    exit 0
fi

# Применение обновлений
log "Applying updates..."
su - "$INSTALL_USER" -c "
    cd $INSTALL_PATH/bot && \
    git pull origin main
" || {
    error "Failed to pull updates"
    warn "You may have local changes. Check:"
    warn "  $backup_dir/git_status.txt"
    warn "  $backup_dir/git_diff.txt"
    log "Starting service with old version..."
    systemctl start "$SERVICE_NAME"
    exit 1
}

# Обновление зависимостей
log "Updating Python dependencies..."
su - "$INSTALL_USER" -c "
    cd $INSTALL_PATH/bot && \
    source ../venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt
" || {
    error "Failed to update dependencies"
    warn "Restoring from backup..."
    rm -rf "$INSTALL_PATH/bot"
    cp -r "$backup_dir/bot_full" "$INSTALL_PATH/bot"
    chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH/bot"
    systemctl start "$SERVICE_NAME"
    error "Update failed, restored from backup"
    exit 1
}

# Восстанавливаем конфиги (на случай если они были в .gitignore и затерлись)
if [ -f "$backup_dir/.env" ] && [ ! -f "$INSTALL_PATH/bot/.env" ]; then
    cp "$backup_dir/.env" "$INSTALL_PATH/bot/.env"
    chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH/bot/.env"
    warn "Restored .env file"
fi

if [ -f "$backup_dir/google_credentials.json" ] && [ ! -f "$INSTALL_PATH/bot/google_credentials.json" ]; then
    cp "$backup_dir/google_credentials.json" "$INSTALL_PATH/bot/google_credentials.json"
    chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_PATH/bot/google_credentials.json"
    warn "Restored google_credentials.json"
fi

# Запуск сервиса
log "Starting service..."
systemctl start "$SERVICE_NAME"

# Проверка статуса
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Update successful! Service is running."
    info "Backup saved at: $backup_dir"
    info "You can check logs with: sudo journalctl -u $SERVICE_NAME -f"
else
    error "Service failed to start after update!"
    warn "Check logs: sudo journalctl -u $SERVICE_NAME -n 50"
    warn "Backup available at: $backup_dir"
    exit 1
fi