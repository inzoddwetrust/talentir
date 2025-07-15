#!/bin/bash

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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
    exit 1
}

# Проверка root прав
if [ "$EUID" -ne 0 ]; then
    error "Please run as root (with sudo)"
fi

# Получение параметров установки
read -p "Enter domain or IP for BookStack: " DOMAIN
read -p "Enter MySQL root password: " MYSQL_ROOT_PASSWORD
read -p "Enter BookStack database name [bookstack]: " DB_NAME
DB_NAME=${DB_NAME:-bookstack}
read -p "Enter BookStack database user [bookstack]: " DB_USER
DB_USER=${DB_USER:-bookstack}
read -p "Enter BookStack database password: " DB_PASSWORD

# Настройка локалей
log "Setting up locales..."
apt install -y locales || error "Failed to install locales"

locale-gen en_US.UTF-8
locale-gen ru_RU.UTF-8
locale-gen de_DE.UTF-8
locale-gen id_ID.UTF-8

cat > /etc/default/locale << EOF
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
LANGUAGE=en_US.UTF-8
EOF

cat >> /etc/environment << EOF
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
LANGUAGE=en_US.UTF-8
EOF

# Обновление системы и установка зависимостей
log "Updating system and installing dependencies..."
apt update || error "Failed to update package list"
apt upgrade -y || warn "Some packages could not be upgraded"

log "Installing required packages..."
apt install -y nginx mariadb-server php-fpm php-mysql php-curl php-xml \
    php-mbstring php-gd php-cli php-tokenizer php-json git curl \
    php8.2-mbstring php8.2-gd php8.2-xml php8.2-mysql php8.2-curl php8.2-zip || \
    error "Failed to install required packages"

# Настройка MariaDB
log "Configuring MariaDB..."
mysql -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';"
mysql -e "GRANT ALL ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';"
mysql -e "FLUSH PRIVILEGES;"

# Установка BookStack
log "Installing BookStack..."
cd /var/www || error "Failed to change directory to /var/www"
git clone https://github.com/BookStackApp/BookStack.git --branch release --single-branch bookstack || \
    error "Failed to clone BookStack repository"

# Настройка прав доступа
log "Setting up permissions..."
chown -R www-data:www-data /var/www/bookstack
chmod -R 755 /var/www/bookstack
chmod -R 775 /var/www/bookstack/storage
chmod -R 775 /var/www/bookstack/bootstrap/cache

# Установка Composer
log "Installing Composer..."
curl -sS https://getcomposer.org/installer | php
mv composer.phar /usr/local/bin/composer

# Установка зависимостей через Composer
log "Installing dependencies..."
cd /var/www/bookstack || error "Failed to change directory to bookstack"
composer install --no-dev || error "Failed to install dependencies"

# Настройка конфигурации
log "Setting up configuration..."
cp .env.example .env || error "Failed to create .env file"

# Обновление .env файла
sed -i "s#APP_URL=.*#APP_URL=http://${DOMAIN}#g" .env
sed -i "s#DB_DATABASE=.*#DB_DATABASE=${DB_NAME}#g" .env
sed -i "s#DB_USERNAME=.*#DB_USERNAME=${DB_USER}#g" .env
sed -i "s#DB_PASSWORD=.*#DB_PASSWORD=${DB_PASSWORD}#g" .env

# Генерация ключа приложения
php artisan key:generate || error "Failed to generate application key"

# Выполнение миграций
log "Running database migrations..."
php artisan migrate --force || error "Failed to run migrations"

# Настройка Nginx
log "Configuring Nginx..."
cat > /etc/nginx/sites-available/bookstack << EOF
server {
    listen 80;
    server_name ${DOMAIN};
    root /var/www/bookstack/public;

    index index.php index.html;
    client_max_body_size 100M;

    access_log /var/log/nginx/bookstack_access.log;
    error_log /var/log/nginx/bookstack_error.log;

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }

    location ~ \.php$ {
        try_files \$uri =404;
        fastcgi_split_path_info ^(.+\.php)(/.+)$;
        fastcgi_pass unix:/var/run/php/php8.2-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param PATH_INFO \$fastcgi_path_info;
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

# Активация конфигурации Nginx
ln -sf /etc/nginx/sites-available/bookstack /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t || error "Nginx configuration test failed"

# Перезапуск сервисов
log "Restarting services..."
systemctl restart php8.2-fpm
systemctl restart nginx

log "Installation completed successfully!"
log "BookStack is now available at: http://${DOMAIN}"
log "Default login credentials:"
log "Email: admin@admin.com"
log "Password: password"
log "Please change these credentials after first login!"