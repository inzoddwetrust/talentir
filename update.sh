#!/bin/bash

INSTALL_PATH="/opt/talentir"
SERVICE_NAME="talentir-bot"

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

# Определение пользователя
if [ -n "$SUDO_USER" ]; then
    INSTALL_USER="$SUDO_USER"
else
    INSTALL_USER="$USER"
fi

# Проверка существования установки
if [ ! -d "$INSTALL_PATH/bot" ]; then
    log "Installation not found at $INSTALL_PATH/bot"
    exit 1
fi

if [ ! -d "$INSTALL_PATH/venv" ]; then
    log "Virtual environment not found at $INSTALL_PATH/venv"
    exit 1
fi

# Создание бэкапа перед обновлением
log "Creating backup..."
backup_dir="$INSTALL_PATH/backup_$(date +'%Y%m%d_%H%M%S')"
cp -r "$INSTALL_PATH/bot" "$backup_dir"
log "Backup created at $backup_dir"

# Остановка сервиса
log "Stopping service..."
systemctl stop "$SERVICE_NAME"

# Получение обновлений и обновление зависимостей
log "Updating code and dependencies..."
su - "$INSTALL_USER" -c "
    cd $INSTALL_PATH/bot && \
    source ../venv/bin/activate && \
    git pull && \
    pip install --upgrade pip && \
    pip install -r requirements.txt
" || {
    log "Update failed, restoring from backup..."
    rm -rf "$INSTALL_PATH/bot"
    mv "$backup_dir" "$INSTALL_PATH/bot"
    systemctl start "$SERVICE_NAME"
    log "Restore complete. Previous version is running."
    exit 1
}

# Запуск сервиса
log "Starting service..."
systemctl start "$SERVICE_NAME"

# Проверка статуса
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Update successful! Service is running."
    log "You can check status with: sudo systemctl status $SERVICE_NAME"
else
    log "Warning: Service failed to start after update."
    log "Please check logs with: sudo journalctl -u $SERVICE_NAME"
    exit 1
fi