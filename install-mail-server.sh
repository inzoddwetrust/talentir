#!/bin/bash

# Установка почтового сервера для Talentir
# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

# Проверка root прав
if [ "$EUID" -ne 0 ]; then
    error "Please run as root (with sudo)"
    exit 1
fi

# Переменные
HOSTNAME="mail.talentir.info"
DOMAIN="talentir.info"
MAIL_USER="noreply"
MAIL_PASSWORD=""

# Генерируем случайный пароль для почтового пользователя
generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-25
}

MAIL_PASSWORD=$(generate_password)

log "Starting mail server installation for $DOMAIN"
log "Hostname: $HOSTNAME"
log "Mail user: $MAIL_USER@$DOMAIN"

# Обновление системы
log "Updating system packages..."
apt update && apt upgrade -y

# Установка необходимых пакетов
log "Installing mail server packages..."
# Устанавливаем без интерактивных вопросов
DEBIAN_FRONTEND=noninteractive apt install -y \
    postfix \
    dovecot-core \
    dovecot-imapd \
    dovecot-pop3d \
    opendkim \
    opendkim-tools \
    postfix-policyd-spf-python \
    certbot \
    sasl2-bin \
    libsasl2-2 \
    libsasl2-modules

# Настройка hostname
log "Setting up hostname..."
hostnamectl set-hostname $HOSTNAME
echo "127.0.0.1 $HOSTNAME" >> /etc/hosts

# Получение SSL сертификата
log "Getting SSL certificate..."
certbot certonly --standalone -d $HOSTNAME --non-interactive --agree-tos --email admin@$DOMAIN

# Настройка Postfix
log "Configuring Postfix..."
cp /etc/postfix/main.cf /etc/postfix/main.cf.backup

cat > /etc/postfix/main.cf << EOF
# Основные настройки
myhostname = $HOSTNAME
mydomain = $DOMAIN
myorigin = \$mydomain
inet_interfaces = all
inet_protocols = ipv4
mydestination = \$myhostname, localhost.\$mydomain, localhost, \$mydomain

# Сети и релеи
mynetworks = 127.0.0.0/8 [::ffff:127.0.0.0]/104 [::1]/128
relayhost =

# Размеры сообщений
message_size_limit = 50000000
mailbox_size_limit = 0

# SASL аутентификация
smtpd_sasl_auth_enable = yes
smtpd_sasl_type = dovecot
smtpd_sasl_path = private/auth
smtpd_sasl_security_options = noanonymous
smtpd_sasl_local_domain = \$myhostname

# TLS/SSL настройки
smtpd_tls_cert_file = /etc/letsencrypt/live/$HOSTNAME/fullchain.pem
smtpd_tls_key_file = /etc/letsencrypt/live/$HOSTNAME/privkey.pem
smtpd_use_tls = yes
smtpd_tls_security_level = encrypt
smtpd_tls_session_cache_database = btree:\${data_directory}/smtpd_scache

# Клиентские TLS настройки
smtp_tls_security_level = may
smtp_tls_session_cache_database = btree:\${data_directory}/smtp_scache

# Ограничения доступа
smtpd_helo_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_invalid_helo_hostname, reject_non_fqdn_helo_hostname
smtpd_sender_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_non_fqdn_sender, reject_unknown_sender_domain
smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_non_fqdn_recipient, reject_unknown_recipient_domain, reject_unauth_destination

# DKIM
milter_default_action = accept
milter_protocol = 2
smtpd_milters = inet:localhost:8891
non_smtpd_milters = inet:localhost:8891

# SPF
policy-spf_time_limit = 3600s
EOF

# Настройка портов в master.cf
log "Configuring Postfix master.cf..."
cp /etc/postfix/master.cf /etc/postfix/master.cf.backup

# Добавляем настройки для submission (port 587)
cat >> /etc/postfix/master.cf << EOF

# Submission port 587
submission inet n       -       y       -       -       smtpd
  -o syslog_name=postfix/submission
  -o smtpd_tls_security_level=encrypt
  -o smtpd_sasl_auth_enable=yes
  -o smtpd_tls_auth_only=yes
  -o smtpd_reject_unlisted_recipient=no
  -o smtpd_client_restrictions=permit_sasl_authenticated,reject
  -o smtpd_helo_restrictions=permit_sasl_authenticated,reject
  -o smtpd_sender_restrictions=permit_sasl_authenticated,reject
  -o smtpd_recipient_restrictions=permit_sasl_authenticated,reject_unauth_destination
  -o milter_macro_daemon_name=ORIGINATING

# SPF policy service
policy-spf  unix  -       n       n       -       0       spawn
    user=postfix argv=/usr/bin/policyd-spf
EOF

# Настройка Dovecot
log "Configuring Dovecot..."
cp /etc/dovecot/dovecot.conf /etc/dovecot/dovecot.conf.backup

cat > /etc/dovecot/dovecot.conf << EOF
# Основные настройки
!include_try /usr/share/dovecot/protocols.d/*.protocol
protocols = imap pop3 lmtp

# Настройки подключения
listen = *
base_dir = /var/run/dovecot/

# Аутентификация
disable_plaintext_auth = no
auth_mechanisms = plain login

# Пользователи
mail_location = mbox:~/mail:INBOX=/var/mail/%u
mail_privileged_group = mail

# SSL
ssl = yes
ssl_cert = </etc/letsencrypt/live/$HOSTNAME/fullchain.pem
ssl_key = </etc/letsencrypt/live/$HOSTNAME/privkey.pem

# Сервис аутентификации для Postfix
service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0666
    user = postfix
    group = postfix
  }
}

# LMTP сервис
service lmtp {
  unix_listener /var/spool/postfix/private/dovecot-lmtp {
    mode = 0600
    user = postfix
    group = postfix
  }
}

passdb {
  driver = pam
}

userdb {
  driver = passwd
}
EOF

# Создание почтового пользователя
log "Creating mail user: $MAIL_USER@$DOMAIN"
if ! id "$MAIL_USER" &>/dev/null; then
    useradd -m -s /bin/bash $MAIL_USER
    echo "$MAIL_USER:$MAIL_PASSWORD" | chpasswd

    # Создаем директорию для почты
    mkdir -p /home/$MAIL_USER/mail
    chown $MAIL_USER:$MAIL_USER /home/$MAIL_USER/mail
    chmod 700 /home/$MAIL_USER/mail

    info "Mail user created with password: $MAIL_PASSWORD"
else
    warn "User $MAIL_USER already exists"
fi

# Настройка OpenDKIM
log "Configuring OpenDKIM..."
mkdir -p /etc/opendkim/keys/$DOMAIN
chown -R opendkim:opendkim /etc/opendkim

cat > /etc/opendkim.conf << EOF
# OpenDKIM Configuration
AutoRestart             Yes
AutoRestartRate         10/1h
LogWhy                  yes
Syslog                  yes
SyslogSuccess           Yes
Mode                    sv
Canonicalization        relaxed/simple
ExternalIgnoreList      refile:/etc/opendkim/TrustedHosts
InternalHosts           refile:/etc/opendkim/TrustedHosts
KeyTable                refile:/etc/opendkim/KeyTable
SigningTable            refile:/etc/opendkim/SigningTable
Socket                  inet:8891@localhost
PidFile                 /var/run/opendkim/opendkim.pid
SignatureAlgorithm      rsa-sha256
UserID                  opendkim:opendkim
UMask                   022
EOF

# Создание файлов конфигурации DKIM
cat > /etc/opendkim/TrustedHosts << EOF
127.0.0.1
localhost
192.168.0.0/16
10.0.0.0/8
172.16.0.0/12
*.$DOMAIN
EOF

cat > /etc/opendkim/KeyTable << EOF
mail._domainkey.$DOMAIN $DOMAIN:mail:/etc/opendkim/keys/$DOMAIN/mail.private
EOF

cat > /etc/opendkim/SigningTable << EOF
*@$DOMAIN mail._domainkey.$DOMAIN
EOF

# Генерация DKIM ключей
log "Generating DKIM keys..."
cd /etc/opendkim/keys/$DOMAIN
opendkim-genkey -s mail -d $DOMAIN
chown opendkim:opendkim mail.private
chmod 600 mail.private

# Показываем DKIM запись для DNS
log "DKIM DNS record to add:"
echo "========================================"
echo "Record Type: TXT"
echo "Name: mail._domainkey.$DOMAIN"
echo "Value:"
cat mail.txt | grep -v "mail._domainkey" | tr -d '\n\t " '
echo ""
echo "========================================"

# Настройка фаервола
log "Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 25/tcp
    ufw allow 587/tcp
    ufw allow 993/tcp
    ufw allow 995/tcp
fi

# Перезапуск сервисов
log "Starting services..."
systemctl enable postfix dovecot opendkim
systemctl restart postfix dovecot opendkim

# Проверка статуса
log "Checking service status..."
if systemctl is-active --quiet postfix; then
    info "✅ Postfix is running"
else
    error "❌ Postfix failed to start"
fi

if systemctl is-active --quiet dovecot; then
    info "✅ Dovecot is running"
else
    error "❌ Dovecot failed to start"
fi

if systemctl is-active --quiet opendkim; then
    info "✅ OpenDKIM is running"
else
    error "❌ OpenDKIM failed to start"
fi

# Создание файла с настройками для бота
cat > /root/mail-server-config.txt << EOF
=== MAIL SERVER CONFIGURATION ===

SMTP Settings for bot:
SMTP_HOST=mail.talentir.info
SMTP_PORT=587
SMTP_USER=$MAIL_USER@$DOMAIN
SMTP_PASSWORD=$MAIL_PASSWORD

DNS Records to add:
1. SPF Record:
   Name: talentir.info
   Type: TXT
   Value: v=spf1 include:spf.mtasv.net mx ~all

2. DMARC Record:
   Name: _dmarc.talentir.info
   Type: TXT
   Value: v=DMARC1; p=none; rua=mailto:dmarc@talentir.info

3. DKIM Record (already generated above)

Test commands:
- Test SMTP: telnet mail.talentir.info 587
- Check logs: tail -f /var/log/mail.log
- Test sending: echo "Test" | mail -s "Test" test@example.com

=== END CONFIGURATION ===
EOF

log "Installation completed!"
log "Configuration saved to: /root/mail-server-config.txt"
warn "Don't forget to add the DNS records shown above!"
info "SMTP credentials: $MAIL_USER@$DOMAIN / $MAIL_PASSWORD"