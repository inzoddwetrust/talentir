import logging
import asyncio
from aiohttp import ClientTimeout
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils.exceptions import NetworkError, TelegramAPIError
from aiogram.dispatcher import FSMContext
from datetime import datetime
from decimal import Decimal
from sqlalchemy import func

from init import get_session, init_tables, Session
from database import User, Project, Option, Purchase, Payment, Bonus, Notification, ActiveBalance, PassiveBalance, \
    Transfer
from templates import MessageTemplates
from fsm_states import ProjectCarouselState, PurchaseFlow, TxidInputState, UserDataDialog, TransferDialog
from txid_checker import validate_txid, verify_transaction, TxidValidationCode
from notificator import NotificationProcessor
from invoice_cleaner import InvoiceCleaner
from userdatamanager import UserDataManager
from variables import GlobalVariables, initialize_variables
from exports import SheetsExporter
from bonus_processor import process_purchase_with_bonuses
from admin_commands import setup_admin_commands
from message_manager import MessageManager
from transfer_manager import TransferManager
from user_decorator import with_user
from bookstack_integration import BookStackManager
from legacy_user_processor import legacy_processor
import config
import helpers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
timeout = ClientTimeout(total=60)
bot = Bot(token=config.API_TOKEN, timeout=timeout)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
BOT_USERNAME = None
message_manager = MessageManager(bot)


# region Start Screen

async def show_welcome_screen(user, message_or_callback, session):
    eula_accepted = helpers.get_user_note(user, 'eula') == '1'

    if not eula_accepted:
        projects_count = await GlobalVariables().get('projectsCount')
        users_count = await GlobalVariables().get('usersCount')
        purchases_total = await GlobalVariables().get('purchasesTotal')

        await message_manager.send_template(
            user=user,
            template_key='/dashboard/newUser',
            update=message_or_callback,
            variables={
                'firstname': user.firstname,
                'language': user.lang or 'en',
                'projectsCount': projects_count,
                'usersCount': users_count,
                'purchasesTotal': purchases_total
            }
        )
        await ProjectCarouselState.wait_for_welcome.set()
        return

    required_channels = await GlobalVariables().get('required_channels')
    if required_channels:  # Если список каналов не пустой
        subscribed, not_subscribed_channels = await helpers.check_user_subscriptions(
            bot, user.telegramID, user.lang or "en"
        )

        if not subscribed:
            # Формируем список каналов для шаблона
            channels = []
            urls = []
            lang_channels = []

            for channel in not_subscribed_channels:
                channels.append(channel["title"])
                urls.append(channel["url"])
                lang_channels.append(channel["title"])

            await message_manager.send_template(
                user=user,
                template_key='/dashboard/noSubscribe',
                update=message_or_callback,
                variables={
                    'firstname': user.firstname,
                    'rgroup': {
                        'channel': channels,
                        'url': urls,
                        'langChannel': lang_channels
                    }
                }
            )
            return

    user_purchases_total = session.query(func.sum(Purchase.packPrice)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    upline_count = session.query(func.count(User.userID)).filter(
        User.upline == user.telegramID
    ).scalar() or 0

    def get_all_referrals(telegram_id, visited=None):
        if visited is None:
            visited = set()

        referrals = session.query(User.telegramID).filter(
            User.upline == telegram_id
        ).all()

        total = 0
        for (ref_id,) in referrals:
            if ref_id not in visited:
                visited.add(ref_id)
                total += 1 + get_all_referrals(ref_id, visited)
        return total

    upline_total = get_all_referrals(user.telegramID)

    # Получаем глобальные переменные
    projects_count = await GlobalVariables().get('projectsCount')
    users_count = await GlobalVariables().get('usersCount')
    purchases_total = await GlobalVariables().get('purchasesTotal')

    await message_manager.send_template(
        user=user,
        template_key='/dashboard/existingUser',
        update=message_or_callback,
        variables={
            'firstname': user.firstname,
            'language': user.lang or 'en',
            'balanceActive': user.balanceActive,
            'balancePassive': user.balancePassive,
            'balance': user.balanceActive + user.balancePassive,
            'projectsCount': projects_count,
            'usersCount': users_count,
            'purchasesTotal': purchases_total,
            'userPurchasesTotal': user_purchases_total,
            'uplineCount': upline_count,
            'uplineTotal': upline_total
        }
    )


async def generate_document(
        message: types.Message,
        document_type: str,  # "certificate" или "purchase"
        id_value: str,  # ID проекта для сертификата или ID покупки для документа покупки
        session=None
):
    """Универсальный обработчик для генерации документов различных типов."""
    close_session = False
    if session is None:
        session = Session()
        close_session = True

    try:
        user, success = await helpers.get_user_from_update(message, session)
        if not success:
            return

        # Определяем тип документа и настраиваем соответствующие переменные
        if document_type == "certificate":
            project_id = id_value
            doc_type = "cert"
            template_prefix = "certificate"
            filename_prefix = "certificate"

            # Проверяем наличие покупок для этого проекта
            purchases = session.query(Purchase).filter(
                Purchase.userID == user.userID,
                Purchase.projectID == project_id
            ).all()

            if not purchases:
                await message_manager.send_template(
                    user=user,
                    template_key=f'{template_prefix}_not_found',
                    update=message
                )
                return

            # Находим проект
            project = session.query(Project).filter(
                Project.projectID == project_id,
                Project.lang == user.lang
            ).first() or session.query(Project).filter(
                Project.projectID == project_id,
                Project.lang == 'en'
            ).first()

            if not project:
                await message_manager.send_template(
                    user=user,
                    template_key=f'{template_prefix}_project_not_found',
                    update=message
                )
                return

            # Вычисляем дополнительные данные для сертификата
            total_shares = sum(purchase.packQty for purchase in purchases)
            latest_purchase = max(purchases, key=lambda p: p.createdAt)

            # Подготавливаем контекст для сертификата
            context = {
                'name': user.firstname,
                'surname': user.surname or "",
                'projectName': project.projectName,
                'certNumber': f"CERT-{project_id}-{user.userID}",
                'date': latest_purchase.createdAt.strftime('%d.%m.%Y'),
                'shares': total_shares
            }

            variables = {
                'projectName': project.projectName
            }

        elif document_type == "purchase":
            purchase_id = id_value
            doc_type = "agreement"
            template_prefix = "purchase_doc"
            filename_prefix = "purchase"

            # Проверяем наличие покупки
            purchase = session.query(Purchase).filter_by(purchaseID=purchase_id).first()
            if not purchase:
                await message_manager.send_template(
                    user=user,
                    template_key=f'{template_prefix}_not_found',
                    update=message
                )
                return

            # Находим проект
            project = session.query(Project).filter(
                Project.projectID == purchase.projectID,
                Project.lang == user.lang
            ).first() or session.query(Project).filter(
                Project.projectID == purchase.projectID,
                Project.lang == 'en'
            ).first()

            # Подготавливаем контекст для документа покупки
            option = session.query(Option).filter_by(optionID=purchase.optionID).first()
            context = {
                # Основная информация
                'docNumber': purchase.purchaseID,
                'date': purchase.createdAt.strftime('%d.%m.%Y'),

                # Данные пользователя
                'firstname': user.firstname,
                'surname': user.surname or '',
                'city': user.city or 'Not provided',
                'country': user.country or 'Not provided',
                'address': user.address or 'Not provided',
                'number': user.passport or 'Not provided',
                'birthday': user.birthday.strftime('%d.%m.%Y') if user.birthday else 'Not provided',
                'email': user.email or 'Not provided',

                # Данные опциона
                'packQty': purchase.packQty,
                'pricePerShare': option.costPerShare,
                'packPrice': purchase.packPrice
            }

            variables = {
                'purchase_id': purchase_id
            }
        else:
            logger.error(f"Unknown document type: {document_type}")
            return

        # Проверяем, что данные пользователя заполнены
        if not user.isFilled:
            await message_manager.send_template(
                user=user,
                template_key='doc_need_data',
                update=message
            )
            return

        # Сообщаем о начале генерации документа
        await message_manager.send_template(
            user=user,
            template_key=f'{template_prefix}_generating',
            update=message,
            variables=variables
        )

        try:
            # Добавляем подробное логирование для отладки
            logger.info(f"Starting {document_type} generation for {id_value}")

            # Получаем HTML из BookStack
            from bookstack_integration import get_document_html, render_document, get_document_as_pdf
            import io

            # Получаем HTML документа
            html = get_document_html(project, doc_type)
            if not html:
                logger.error(f"No HTML content found for project {project.projectID}")
                raise FileNotFoundError(f"Template not found for {document_type} {id_value}")

            logger.info(f"Got HTML content, length: {len(html)}")

            # Рендерим шаблон
            rendered_html = render_document(html, context)
            logger.info(f"Rendered HTML, length: {len(rendered_html)}")

            # Генерируем PDF
            pdf_bytes = get_document_as_pdf(rendered_html)
            if not pdf_bytes:
                logger.error("Failed to get PDF bytes")
                raise RuntimeError("PDF generation failed")

            logger.info(f"Generated PDF bytes: {len(pdf_bytes)} bytes")

            # Создаем документ
            document = types.InputFile(io.BytesIO(pdf_bytes), filename=f"{filename_prefix}_{id_value}.pdf")

            # Отправляем документ
            await message.answer_document(document=document)

            # Сообщаем об успешной генерации
            await message_manager.send_template(
                user=user,
                template_key=f'{template_prefix}_ready',
                variables=variables,
                update=message
            )

        except FileNotFoundError as e:
            logging.error(f"Template not found for {document_type} {id_value}: {e}")
            await message_manager.send_template(
                user=user,
                template_key=f'{template_prefix}_error',
                update=message
            )

        except Exception as e:
            logging.error(f"Error generating {document_type} for {id_value}: {e}", exc_info=True)
            await message_manager.send_template(
                user=user,
                template_key=f'{template_prefix}_generation_error',
                update=message
            )

    finally:
        if close_session:
            session.close()


@dp.message_handler(commands=['start'], state="*")
async def send_welcome(message: types.Message, state: FSMContext):
    start_payload = message.get_args()

    with Session() as session:
        user, success = await helpers.get_user_from_update(message, session)
        if not success:
            if start_payload and start_payload.isdigit():
                referrer_id = int(start_payload)
            else:
                referrer_id = config.DEFAULT_REFERRER_ID

            if referrer_id == message.from_user.id:
                referrer_id = config.DEFAULT_REFERRER_ID

            referrer = session.query(User).filter_by(telegramID=referrer_id).first()
            if not referrer:
                referrer_id = config.DEFAULT_REFERRER_ID

            user = User.create_from_telegram_data(session, message.from_user)
            user.upline = referrer_id
            session.commit()

            # Важно: получаем свежий объект пользователя после commit
            user = session.query(User).filter_by(telegramID=message.from_user.id).first()

        if start_payload and start_payload.startswith("invoice_"):
            invoice_id = start_payload.split("_")[1]
            payment = session.query(Payment).filter_by(paymentID=invoice_id, status="pending").first()

            if payment and payment.userID == user.userID:
                await message_manager.send_template(
                    user=user,
                    template_key='pending_invoice_details',
                    update=message,
                    variables={
                        'amount': payment.amount,
                        'method': payment.method,
                        'sumCurrency': payment.sumCurrency,
                        'wallet': payment.toWallet or config.WALLETS.get(payment.method),
                        'payment_id': payment.paymentID
                    }
                )
                return

        if start_payload and start_payload.startswith("purchase_"):
            purchase_id = start_payload.split("_")[1]
            await generate_document(message, "purchase", purchase_id)
            return

        if start_payload and start_payload.startswith("certificate_"):
            project_id = start_payload.split("_")[1]
            await generate_document(message, "certificate", project_id)
            return

        if start_payload and start_payload.startswith("emailverif_"):
            token = start_payload.split("_")[1]
            stored_token = helpers.get_user_note(user, 'verificationToken')
            email_confirmed = helpers.get_user_note(user, 'emailConfirmed')

            if email_confirmed == '1':
                # Уже верифицирован
                await message_manager.send_template(
                    user=user,
                    template_key='/dashboard/emailverif_already',
                    update=message,
                    variables={'email': user.email}
                )
                return

            if stored_token and stored_token == token:
                # Успешная верификация
                helpers.set_user_note(user, 'emailConfirmed', '1')
                session.commit()

                await message_manager.send_template(
                    user=user,
                    template_key='/dashboard/emailverif',
                    update=message,
                    variables={'email': user.email}
                )
            else:
                # Неверный токен
                await message_manager.send_template(
                    user=user,
                    template_key='/dashboard/emailverif_invalid',
                    update=message
                )
            return

        await show_welcome_screen(user, message, session)


@dp.callback_query_handler(lambda c: c.data == "/check/subscription", state="*")
@with_user
async def check_subscription_handler(user: User, callback_query: types.CallbackQuery, session: Session):
    subscribed, not_subscribed_channels = await helpers.check_user_subscriptions(
        bot, user.telegramID, user.lang or "en"
    )

    if subscribed:
        await show_welcome_screen(user, callback_query, session)
    else:
        channels = []
        urls = []
        lang_channels = []

        for channel in not_subscribed_channels:
            channels.append(channel["title"])
            urls.append(channel["url"])
            lang_channels.append(channel["title"])

        await message_manager.send_template(
            user=user,
            template_key='/dashboard/noSubscribeRepeat',
            update=callback_query,
            variables={
                'firstname': user.firstname,
                'rgroup': {
                    'channel': channels,
                    'url': urls,
                    'langChannel': lang_channels
                }
            },
            delete_original=True
        )


@dp.callback_query_handler(lambda c: c.data.startswith('lang_'), state="*")
async def handle_language_select(callback_query: types.CallbackQuery):
    lang = callback_query.data.split('_')[1]

    with Session() as session:
        user = session.query(User).filter_by(telegramID=callback_query.from_user.id).first()
        if user:
            if user.lang == lang:
                return

            user.lang = lang
            session.commit()

            await callback_query.message.delete()
            await show_welcome_screen(user, callback_query, session)


@dp.callback_query_handler(lambda c: c.data == '/acceptEula', state="*")
@with_user
async def handle_eula_accept(user: User, callback_query: types.CallbackQuery, session: Session):
    helpers.set_user_note(user, 'eula', '1')
    session.commit()

    await helpers.safe_delete_message(callback_query)
    await show_welcome_screen(user, callback_query, session)


# endregion

# region Портфель
@dp.callback_query_handler(lambda c: c.data == "/case", state="*")
@with_user
async def handle_case(user: User, callback_query: types.CallbackQuery, session: Session):
    """Handles investment portfolio display"""
    # Считаем общую стоимость всех покупок
    user_purchases_total = session.query(func.sum(Purchase.packPrice)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    # Считаем общее количество акций (суммируем packQty)
    user_purchases_qty = session.query(func.sum(Purchase.packQty)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    # Считаем количество уникальных проектов
    user_projects_total = session.query(func.count(func.distinct(Purchase.projectID))).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    await message_manager.send_template(
        user=user,
        template_key='/case',
        update=callback_query,
        variables={
            'userPurchasesQty': user_purchases_qty,
            'userPurchasesTotal': user_purchases_total,
            'userProjectsTotal': user_projects_total
        },
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/case/purchases", state="*")
@with_user
async def my_options_handler(user: User, callback_query: types.CallbackQuery, session: Session):
    purchases = session.query(Purchase).filter_by(userID=user.userID).all()

    if purchases:
        template_key = '/case/purchases'
        doc_links = []

        for purchase in purchases:
            link = f"https://t.me/{BOT_USERNAME}?start=purchase_{purchase.purchaseID}"
            doc_links.append(f"<a href='{link}'>PDF</a>")

        context = {
            "rgroup": {
                'i': list(range(1, len(purchases) + 1)),
                'projectName': [p.projectName for p in purchases],
                'shares': [p.packQty for p in purchases],
                'price': [p.packPrice for p in purchases],
                'date': [p.createdAt.strftime('%Y-%m-%d') for p in purchases],
                'PDF': doc_links
            }
        }
    else:
        template_key = '/case/purchases/empty'
        context = {}

    await message_manager.send_template(
        user=user,
        template_key=template_key,
        variables=context,
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/case/certs", state="*")
@with_user
async def handle_certificates(user: User, callback_query: types.CallbackQuery, session: Session):
    purchases = session.query(Purchase).filter_by(userID=user.userID).all()

    if not purchases:
        await message_manager.send_template(
            user=user,
            template_key='/case/certs/empty',
            update=callback_query,
            delete_original=True
        )
        return

    project_aggregates = {}
    for purchase in purchases:
        project_id = purchase.projectID
        if project_id not in project_aggregates:
            project_aggregates[project_id] = {
                'project_id': project_id,
                'project_name': purchase.projectName,
                'total_shares': 0,
                'total_value': 0.0,
                'first_purchase_date': purchase.createdAt,
                'latest_purchase_date': purchase.createdAt
            }

        project_aggregates[project_id]['total_shares'] += purchase.packQty
        project_aggregates[project_id]['total_value'] += purchase.packPrice

        # Обновляем даты первой и последней покупки
        if purchase.createdAt < project_aggregates[project_id]['first_purchase_date']:
            project_aggregates[project_id]['first_purchase_date'] = purchase.createdAt
        if purchase.createdAt > project_aggregates[project_id]['latest_purchase_date']:
            project_aggregates[project_id]['latest_purchase_date'] = purchase.createdAt

    # Преобразуем словарь в список для рендеринга
    aggregates_list = list(project_aggregates.values())

    # Формируем данные для шаблона
    certificate_links = []
    project_names = []
    total_shares = []
    total_values = []
    dates = []

    for agg in aggregates_list:
        # Ссылка для скачивания сертификата
        link = f"https://t.me/{BOT_USERNAME}?start=certificate_{agg['project_id']}"
        certificate_links.append(f"<a href='{link}'>PDF</a>")

        project_names.append(agg['project_name'])
        total_shares.append(agg['total_shares'])
        total_values.append(agg['total_value'])
        dates.append(agg['latest_purchase_date'].strftime('%Y-%m-%d'))

    context = {
        "rgroup": {
            'i': list(range(1, len(aggregates_list) + 1)),
            'projectName': project_names,
            'shares': total_shares,
            'price': total_values,
            'date': dates,
            'PDF': certificate_links
        }
    }

    await message_manager.send_template(
        user=user,
        template_key='/case/certs',
        variables=context,
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/case/strategies", state="*")
@with_user
async def handle_strategies(user: User, callback_query: types.CallbackQuery, session: Session):
    current_strategy = helpers.get_user_note(user, 'strategy') or "manual"
    template_keys = ['/case/strategies', f'/case/strategies/{current_strategy}']

    await message_manager.send_template(
        user=user,
        template_key=template_keys,
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data.startswith("/case/strategies/set_"), state="*")
@with_user
async def set_strategy(user: User, callback_query: types.CallbackQuery, session: Session):
    """Обработчик для выбора стратегии"""
    strategy_key = callback_query.data.split("_")[1]  # например, "manual", "safe"

    current_strategy = helpers.get_user_note(user, 'strategy') or "manual"

    helpers.set_user_note(user, 'strategy', strategy_key)
    session.commit()

    if current_strategy == strategy_key:
        await callback_query.answer("Эта стратегия уже выбрана")
        return

    template_keys = ['/case/strategies', f'/case/strategies/{strategy_key}']

    try:
        await message_manager.send_template(
            user=user,
            template_key=template_keys,
            update=callback_query,
            edit=True
        )
    except Exception as e:
        logger.warning(f"Error updating strategy message: {e}")


@dp.callback_query_handler(lambda c: c.data == "/case/value", state="*")
@with_user
async def handle_portfolio_value(user: User, callback_query: types.CallbackQuery, session: Session):
    strategy = helpers.get_user_note(user, 'strategy') or "manual"

    strategy_coefficients = await GlobalVariables().get('strategy_coefficients') or {}
    coefficient = strategy_coefficients.get(strategy, 1.0)

    user_purchases_total = session.query(func.sum(Purchase.packPrice)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    projected_value = user_purchases_total * coefficient

    user_shares_total = session.query(func.sum(Purchase.packQty)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    growth_percent = (coefficient) * 100

    template_keys = [f'portfolio_value_strategy_{strategy}']

    if strategy == "manual":
        template_keys.append('portfolio_value_manual')
    else:
        template_keys.append('portfolio_value_info')

    template_keys.append('portfolio_value_back')

    await message_manager.send_template(
        user=user,
        template_key=template_keys,
        variables={
            'current_value': user_purchases_total,
            'projected_value': projected_value,
            'growth_percent': growth_percent,
            'total_shares': user_shares_total
        },
        update=callback_query,
        delete_original=True
    )


# endregion

# region Finances

@dp.callback_query_handler(lambda c: c.data == "/finances", state="*")
@with_user
async def finances(user: User, callback_query: types.CallbackQuery, session: Session):
    user_purchases_total = session.query(func.sum(Purchase.packPrice)).filter(
        Purchase.userID == user.userID
    ).scalar() or 0

    user_payments_total = session.query(func.sum(Payment.amount)).filter(
        Payment.userID == user.userID,
        Payment.status == 'paid'
    ).scalar() or 0

    user_bonuses_total = session.query(func.sum(Bonus.bonusAmount)).filter(
        Bonus.userID == user.userID,
        Bonus.status == 'paid'
    ).scalar() or 0

    await message_manager.send_template(
        user=user,
        template_key='/finances',
        update=callback_query,
        variables={
            'balanceActive': user.balanceActive,
            'balancePassive': user.balancePassive,
            'firstname': user.firstname,
            'userid': user.userID,
            'surname': user.surname or '',
            'userPurchasesTotal': user_purchases_total,
            'userPaymentsTotal': user_payments_total,
            'userBonusesTotal': user_bonuses_total
        },
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data in ["active_balance", "passive_balance"], state="*")
@with_user
async def handle_balance(user: User, callback_query: types.CallbackQuery, session: Session):
    """Handles both active and passive balance section display"""
    # Определяем тип баланса из callback_data
    balance_type = callback_query.data  # "active_balance" или "passive_balance"

    # Получаем значение баланса в зависимости от типа
    balance_value = user.balanceActive if balance_type == "active_balance" else user.balancePassive

    await message_manager.send_template(
        user=user,
        template_key=balance_type,  # имя шаблона совпадает с callback_data
        variables={'userid': user.userID, 'balance': balance_value},
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data.startswith(("ab_history", "pb_history")), state="*")
@with_user
async def handle_balance_history(user: User, callback_query: types.CallbackQuery, session: Session):
    callback_data = callback_query.data
    balance_type = "active" if callback_data.startswith("ab_") else "passive"
    operation_type = callback_data.split("_")[-1] if len(callback_data.split("_")) > 2 else None

    if not operation_type or operation_type == "history":
        operation_type = "payments" if balance_type == "active" else "bonuses"

    context = {
        f"balance{balance_type.capitalize()}": getattr(user, f"balance{balance_type.capitalize()}"),
        "active_tab": operation_type
    }

    BalanceModel = ActiveBalance if balance_type == "active" else PassiveBalance

    query_filters = [BalanceModel.userID == user.userID]

    if balance_type == "active":
        if operation_type == "payments":
            query_filters.append(BalanceModel.reason.like('payment=%'))
        elif operation_type == "purchases":
            query_filters.append(BalanceModel.reason.like('purchase=%'))
        elif operation_type == "transfers":
            query_filters.append(BalanceModel.reason.like('transfer=%'))
    else:  # passive balance
        if operation_type == "bonuses":
            query_filters.append(BalanceModel.reason.like('bonus=%'))
        elif operation_type == "transfers":
            query_filters.append(BalanceModel.reason.like('transfer=%'))
        elif operation_type == "others":
            query_filters.append(~BalanceModel.reason.like('bonus=%'))
            query_filters.append(~BalanceModel.reason.like('transfer=%'))

    records = session.query(BalanceModel).filter(
        *query_filters
    ).order_by(BalanceModel.createdAt.desc()).limit(10).all()

    template_prefix = f"{balance_type}_balance_history"
    template_key = f"{template_prefix}_{operation_type}"
    empty_template_key = f"{template_prefix}_empty_{operation_type}"

    if records:
        dates = []
        amounts = []
        statuses = []
        doc_ids = []

        for record in records:
            # Format date
            dates.append(record.createdAt.strftime('%Y-%m-%d %H:%M'))

            # Format amount with emoji indicators
            amount_str = f"{abs(record.amount):.2f}"  # Добавляем форматирование с двумя знаками после запятой
            if record.amount >= 0:
                amounts.append(f"+{amount_str} 💚")  # Green heart for positive
            else:
                amounts.append(f"-{amount_str} ❤️")  # Red heart for negative

            # Status with emoji
            if record.status == 'done':
                statuses.append("✅")
            elif record.status == 'pending':
                statuses.append("⏳")
            elif record.status == 'failed':
                statuses.append("❌")
            else:
                statuses.append(record.status)

            # Извлекаем номер документа из reason
            doc_id = "—"
            if record.reason and '=' in record.reason:
                doc_id = record.reason.split('=')[1]
            doc_ids.append(doc_id)

        context["rgroup"] = {
            'date': dates,
            'amount': amounts,
            'status': statuses,
            'doc_id': doc_ids
        }

        await message_manager.send_template(
            user=user,
            template_key=template_key,
            update=callback_query,
            variables=context,
            delete_original=True
        )
    else:
        await message_manager.send_template(
            user=user,
            template_key=empty_template_key,
            update=callback_query,
            variables=context,
            delete_original=True
        )


@dp.callback_query_handler(lambda c: c.data == "payout", state="*")
@with_user
async def handle_payout(user: User, callback_query: types.CallbackQuery, session: Session):
    await message_manager.send_template(
        user=user,
        template_key='fallback',
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "pending_invoices", state="*")
@with_user
async def pending_invoices_handler(user: User, callback_query: types.CallbackQuery, session: Session):
    check_invoices = session.query(Payment).filter(
        Payment.userID == user.userID,
        Payment.status == 'check'
    ).order_by(Payment.createdAt.desc()).all()

    pending_invoices = session.query(Payment).filter(
        Payment.userID == user.userID,
        Payment.status == 'pending'
    ).order_by(Payment.createdAt.desc()).all()

    invoices = check_invoices + pending_invoices
    invoices = invoices[:10]

    if invoices:
        amounts = []
        info_list = []

        for invoice in invoices:
            # Форматированная сумма с учетом ссылки
            if invoice.status == 'pending':
                link = f"https://t.me/{BOT_USERNAME}?start=invoice_{invoice.paymentID}"
                amounts.append(f"<a href='{link}'>${invoice.amount:.2f}</a>")
            else:
                amounts.append(f"${invoice.amount:.2f}")

            if invoice.status == 'check':
                info_list.append("💰<b>UNDER REVIEW</b>💰")
            else:
                info_list.append(invoice.createdAt.strftime('%Y-%m-%d %H:%M'))

        context = {
            "rgroup": {
                'i': list(range(1, len(invoices) + 1)),
                'amount_str': amounts,
                'method': [inv.method for inv in invoices],
                'sumCurrency': [inv.sumCurrency for inv in invoices],
                'info': info_list
            }
        }
        template_key = 'pending_invoices_list'
    else:
        context = {}
        template_key = 'pending_invoices_empty'

    await message_manager.send_template(
        user=user,
        template_key=template_key,
        update=callback_query,
        variables=context,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "paid_invoices", state="*")
@with_user
async def paid_invoices_handler(user: User, callback_query: types.CallbackQuery, session: Session):
    paid_invoices = session.query(Payment).filter(
        Payment.userID == user.userID,
        Payment.status == 'paid'
    ).order_by(Payment.createdAt.desc()).limit(10).all()

    if paid_invoices:
        info_list = []

        for invoice in paid_invoices:
            if invoice.confirmationTime:
                info_list.append(invoice.confirmationTime.strftime('%Y-%m-%d %H:%M'))
            else:
                info_list.append(invoice.createdAt.strftime('%Y-%m-%d %H:%M'))

        context = {
            "rgroup": {
                'i': list(range(1, len(paid_invoices) + 1)),
                'amount': [inv.amount for inv in paid_invoices],
                'method': [inv.method for inv in paid_invoices],
                'sumCurrency': [inv.sumCurrency for inv in paid_invoices],
                'info': info_list
            }
        }
        template_key = 'paid_invoices_list'
    else:
        context = {}
        template_key = 'paid_invoices_empty'

    await message_manager.send_template(
        user=user,
        template_key=template_key,
        update=callback_query,
        variables=context,
        delete_original=True
    )


# endregion

# region Transfer Dialog
@dp.callback_query_handler(lambda c: c.data == "transfer", state="*")
async def transfer_start(callback_query: types.CallbackQuery, state: FSMContext):
    await TransferManager.start_transfer_dialog(callback_query, state)


@dp.callback_query_handler(
    lambda c: c.data.startswith("transfer_from_") or
              c.data.startswith("transfer_passive_to_") or
              c.data == "transfer_cancel",
    state=TransferDialog.states)
async def handle_transfer_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await TransferManager.handle_callback(callback_query, state)


@dp.message_handler(state=TransferDialog.states, content_types=types.ContentTypes.TEXT)
async def handle_transfer_input(message: types.Message, state: FSMContext):
    await TransferManager.process_input(message, state)


@dp.callback_query_handler(lambda c: c.data == "transfer_execute", state=TransferDialog.confirm_transfer)
async def confirm_transfer(callback_query: types.CallbackQuery, state: FSMContext):
    with Session() as session:
        try:
            sender, success = await helpers.get_user_from_update(callback_query, session)
            if not success:
                return

            transfer_data = await state.get_data()

            source_balance = transfer_data.get('source_balance')
            recipient_id = transfer_data.get('recipient_id')
            amount = transfer_data.get('amount')
            recipient_amount = transfer_data.get('recipient_amount')

            session.begin_nested()

            sender_db = session.query(User).filter_by(userID=sender.userID).with_for_update().first()
            recipient = session.query(User).filter_by(userID=recipient_id).with_for_update().first()

            if not sender_db or not recipient:
                raise ValueError("Отправитель или получатель не найден")

            # Проверяем баланс еще раз перед переводом
            if source_balance == "active" and sender_db.balanceActive < amount:
                raise ValueError("Недостаточно средств на активном балансе")

            if source_balance == "passive" and sender_db.balancePassive < amount:
                raise ValueError("Недостаточно средств на пассивном балансе")

            transfer = Transfer(
                senderUserID=sender.userID,
                senderFirstname=sender_db.firstname,
                senderSurname=sender_db.surname,
                fromBalance=source_balance,
                amount=amount,
                recieverUserID=recipient_id,
                receiverFirstname=recipient.firstname,
                receiverSurname=recipient.surname,
                toBalance="active",  # Всегда на активный баланс
                status="done",
                notes=f"Transfer from {source_balance} balance"
            )
            session.add(transfer)
            session.flush()

            if source_balance == "active":
                sender_db.balanceActive -= amount

                sender_record = ActiveBalance(
                    userID=sender.userID,
                    firstname=sender_db.firstname,
                    surname=sender_db.surname,
                    amount=-amount,
                    status='done',
                    reason=f'transfer={transfer.transferID}',
                    notes=f'Transfer to user {recipient_id}'
                )
                session.add(sender_record)

            else:  # source_balance == "passive"
                sender_db.balancePassive -= amount

                sender_record = PassiveBalance(
                    userID=sender.userID,
                    firstname=sender_db.firstname,
                    surname=sender_db.surname,
                    amount=-amount,
                    status='done',
                    reason=f'transfer={transfer.transferID}',
                    notes=f'Transfer to user {recipient_id}'
                )
                session.add(sender_record)

            # Получатель всегда получает на активный баланс
            recipient.balanceActive += recipient_amount

            recipient_record = ActiveBalance(
                userID=recipient_id,
                firstname=recipient.firstname,
                surname=recipient.surname,
                amount=recipient_amount,
                status='done',
                reason=f'transfer={transfer.transferID}',
                notes=f'Transfer from user {sender.userID}'
            )
            session.add(recipient_record)

            # Если получатель - это другой пользователь, отправляем ему уведомление
            if sender.userID != recipient_id:
                # Маскируем имя отправителя для безопасности (импортируем mask_name из transfer_manager)
                from transfer_manager import mask_name
                masked_first_name = mask_name(sender_db.firstname)
                masked_surname = mask_name(sender_db.surname) if sender_db.surname else ""
                masked_sender_name = f"{masked_first_name} {masked_surname}".strip()

                extra_bonus_text = ""
                if source_balance == 'passive':
                    extra_bonus_text = f"+{config.TRANSFER_BONUS}%"

                text, buttons = await MessageTemplates.get_raw_template(
                    'transfer_received_notification',
                    {
                        'sender_name': masked_sender_name,
                        'sender_id': sender.userID,
                        'amount': recipient_amount,
                        'extra_bonus_text': extra_bonus_text  # Передаем уже подготовленный текст
                    },
                    lang=recipient.lang  # Используем язык получателя для локализации
                )

                notification = Notification(
                    source="transfer",
                    text=text,
                    buttons=buttons,  # Добавляем кнопки из шаблона
                    target_type="user",
                    target_value=str(recipient_id),
                    priority=2,
                    category="transfer",
                    importance="high",
                    parse_mode="HTML"
                )
                session.add(notification)

            session.commit()

            await message_manager.send_template(
                user=sender,
                template_key='transfer_success',
                update=callback_query,
                variables={
                    'sender_name': f"{sender_db.firstname} {sender_db.surname or ''}".strip(),
                    'sender_id': sender.userID,
                    'recipient_name': f"{recipient.firstname} {recipient.surname or ''}".strip(),
                    'recipient_id': recipient_id,
                    'amount': amount,
                    'recipient_amount': recipient_amount,
                    'source_balance': source_balance,
                    'balanceActive': sender_db.balanceActive,
                    'balancePassive': sender_db.balancePassive
                },
                edit=True
            )

            await state.finish()

        except Exception as e:
            # В случае ошибки откатываем транзакцию
            session.rollback()
            logging.error(f"Error executing transfer: {e}")

            await message_manager.send_template(
                user=sender,
                template_key='transfer_error',
                update=callback_query,
                variables={'error_message': str(e)},
                edit=True
            )

            await state.finish()


# endregion

# region Filling user data
@dp.callback_query_handler(lambda c: c.data == "fill_user_data", state="*")
async def fill_user_data(callback_query: types.CallbackQuery, state: FSMContext):
    await UserDataManager.start_user_data_dialog(callback_query, state)


@dp.message_handler(state=UserDataDialog.states, content_types=types.ContentTypes.TEXT)
async def handle_user_data_input(message: types.Message, state: FSMContext):
    await UserDataManager.process_input(message, state)


@dp.callback_query_handler(lambda c: c.data == "confirm_user_data", state=UserDataDialog.waiting_for_confirmation)
async def confirm_user_data(callback_query: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

        try:
            user.firstname = user_data.get('firstname')
            user.surname = user_data.get('surname')
            user.birthday = user_data.get('birthday')
            user.passport = user_data.get('passport')
            user.country = user_data.get('country')
            user.city = user_data.get('city')
            user.address = user_data.get('address')
            user.phoneNumber = user_data.get('phoneNumber')
            user.email = user_data.get('email')
            user.isFilled = True

            # Импортируем функцию из userdatamanager
            from userdatamanager import generate_verification_token
            from email_sender import email_manager

            # Генерируем токен верификации
            verification_token = generate_verification_token()
            verification_link = f"https://t.me/{BOT_USERNAME}?start=emailverif_{verification_token}"

            # Сохраняем токен и статус верификации в notes
            helpers.set_user_note(user, 'emailConfirmed', '0')
            helpers.set_user_note(user, 'verificationToken', verification_token)

            session.commit()

            # Отправляем email с верификацией
            email_sent = await email_manager.send_verification_email(user, verification_link)

            if email_sent:
                # Set timestamp of email sending for cooldown tracking
                helpers.set_email_last_sent(user, datetime.utcnow())

                await message_manager.send_template(
                    user=user,
                    template_key='user_data_saved_email_sent',
                    update=callback_query,
                    variables={'email': user.email},
                    edit=True
                )
            else:
                # Если email не отправлен, все равно сохраняем данные
                await message_manager.send_template(
                    user=user,
                    template_key='user_data_saved_email_failed',
                    update=callback_query,
                    edit=True
                )

            await state.finish()

        except Exception as e:
            session.rollback()
            logging.error(f"Error saving user data: {e}")

            await message_manager.send_template(
                user=user,
                template_key='user_data_save_error',
                update=callback_query,
                edit=True
            )

@dp.callback_query_handler(lambda c: c.data == "restart_user_data", state=UserDataDialog.states)
async def restart_user_data(callback_query: types.CallbackQuery, state: FSMContext):
    await UserDataManager.handle_navigation(callback_query, state, 'restart')


@dp.callback_query_handler(lambda c: c.data == "back", state=UserDataDialog.states)
async def go_back(callback_query: types.CallbackQuery, state: FSMContext):
    await UserDataManager.handle_navigation(callback_query, state, 'back')


@dp.callback_query_handler(lambda c: c.data == "cancel_user_data", state=UserDataDialog.states)
async def cancel_user_data(callback_query: types.CallbackQuery, state: FSMContext):
    await UserDataManager.handle_navigation(callback_query, state, 'cancel')


@dp.callback_query_handler(lambda c: c.data == "edit_user_data", state="*")
@with_user
async def edit_user_data(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    """Handle edit user data request - restart user data collection dialog"""

    # Check if user data is filled but email not confirmed
    if not user.isFilled or helpers.is_email_confirmed(user):
        await callback_query.answer("Invalid request", show_alert=True)
        return

    # Start user data collection dialog from the beginning
    await UserDataManager.start_user_data_dialog(callback_query, state)


@dp.callback_query_handler(lambda c: c.data == "resend_verification_email", state="*")
@with_user
async def resend_verification_email(user: User, callback_query: types.CallbackQuery, session: Session):
    """Handle resend verification email request with cooldown check"""

    # Check if user data is filled but email not confirmed
    if not user.isFilled or helpers.is_email_confirmed(user):
        await callback_query.answer("Invalid request", show_alert=True)
        return

    # Check cooldown
    can_send, remaining_seconds = helpers.can_resend_email(user, cooldown_minutes=5)

    if not can_send:
        remaining_minutes = remaining_seconds // 60 + (1 if remaining_seconds % 60 else 0)
        await message_manager.send_template(
            user=user,
            template_key='email_resend_cooldown',
            update=callback_query,
            variables={'remaining_minutes': remaining_minutes},
            delete_original=True  # <-- Удаляем медиа-сообщение вместо редактирования
        )
        return

    # Get or generate verification token
    verification_token = helpers.get_user_note(user, 'verificationToken')

    # ВАЖНО: Для legacy пользователей генерируем новый токен
    if not verification_token:
        from userdatamanager import generate_verification_token
        verification_token = generate_verification_token()
        helpers.set_user_note(user, 'verificationToken', verification_token)
        helpers.set_user_note(user, 'emailConfirmed', '0')  # Убеждаемся, что статус правильный
        session.commit()

    # Send verification email
    from email_sender import email_manager
    verification_link = f"https://t.me/{BOT_USERNAME}?start=emailverif_{verification_token}"

    email_sent = await email_manager.send_verification_email(user, verification_link)

    if email_sent:
        # Update last sent timestamp
        helpers.set_email_last_sent(user, datetime.utcnow())
        session.commit()

        await message_manager.send_template(
            user=user,
            template_key='email_resend_success',
            update=callback_query,
            variables={'email': user.email},
            delete_original=True  # <-- Удаляем медиа-сообщение вместо редактирования
        )
    else:
        await message_manager.send_template(
            user=user,
            template_key='email_resend_failed',
            update=callback_query,
            delete_original=True  # <-- Удаляем медиа-сообщение вместо редактирования
        )


# endregion

# region Carousel
async def get_project_by_id(session: Session, project_id: int, user_lang: str):
    """Получает проект по ID с учетом языка пользователя и проверкой статуса"""

    # Пробуем найти на языке пользователя
    project = session.query(Project).filter(
        Project.projectID == project_id,
        Project.lang == user_lang
    ).first()

    # Если нашли, проверяем статус
    if project:
        if project.status in ['active', 'child']:
            return project
        else:
            return None  # Проект выключен

    # Если не нашли на языке пользователя, пробуем английский
    project = session.query(Project).filter(
        Project.projectID == project_id,
        Project.lang == 'en'
    ).first()

    # Проверяем статус английской версии
    if project and project.status in ['active', 'child']:
        return project

    return None


@dp.callback_query_handler(lambda c: c.data == "/projects", state="*")
@with_user
async def start_carousel(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    """Starts project carousel from the first project."""
    if callback_query.message:
        await helpers.safe_delete_message(callback_query)

    sorted_projects = await GlobalVariables().get('sorted_projects')
    if not sorted_projects:
        await message_manager.send_template(
            user=user,
            template_key='/projects/notFound',
            update=callback_query
        )
        return

    first_project_id = sorted_projects[0]
    project = await get_project_by_id(session, first_project_id, user.lang)

    if not project:
        await message_manager.send_template(
            user=user,
            template_key='/projects/details/notFound',
            update=callback_query
        )
        return

    await state.update_data(current_project_id=first_project_id)

    await message_manager.send_template(
        user=user,
        template_key='/projects',
        variables={
            'projectName': project.projectName,
            'projectTitle': project.projectTitle,
            'projectID': project.projectID
        },
        update=callback_query,
        override_media_id=project.linkImage
    )

    await ProjectCarouselState.current_project_index.set()


@dp.callback_query_handler(lambda c: c.data.startswith("move_"), state=ProjectCarouselState.current_project_index)
@with_user
async def move_project(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    step = int(callback_query.data.split("_")[1])
    user_data = await state.get_data()
    current_project_id = user_data.get('current_project_id', 0)

    sorted_projects = await GlobalVariables().get('sorted_projects')

    try:
        current_index = sorted_projects.index(current_project_id)
        new_index = (current_index + step) % len(sorted_projects)
        new_project_id = sorted_projects[new_index]
    except ValueError:
        await callback_query.answer("Error: Project not found")
        return

    project = await get_project_by_id(session, new_project_id, user.lang)

    if not project:
        await callback_query.answer("Error: Project not found")
        return

    await state.update_data(current_project_id=new_project_id)

    try:
        await message_manager.send_template(
            user=user,
            template_key='/projects',
            variables={
                'projectName': project.projectName,
                'projectTitle': project.projectTitle,
                'projectID': project.projectID
            },
            update=callback_query,
            edit=True,
            override_media_id=project.linkImage
        )
    except Exception as e:
        logging.error(f"Error updating carousel: {e}")
        await callback_query.answer("Error updating message")


@dp.callback_query_handler(lambda c: c.data == "details", state=ProjectCarouselState.current_project_index)
@with_user
async def view_project_details(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    user_data = await state.get_data()
    current_project_id = user_data.get('current_project_id')

    project = await get_project_by_id(session, current_project_id, user.lang)

    if not project:
        await message_manager.send_template(
            user=user,
            template_key='/projects/notFound',
            update=callback_query
        )
        return

    try:
        await message_manager.send_template(
            user=user,
            template_key='/projects/details',
            variables={
                'projectName': project.projectName,
                'projectDescription': project.fullText,
                'projectID': project.projectID,
                'currentPosition': current_project_id
            },
            update=callback_query,
            edit=True,
            delete_original=bool(project.linkVideo),
            override_media_id=project.linkVideo or project.linkImage,
            media_type='video' if project.linkVideo else None
        )
    except Exception as e:
        logging.error(f"Error showing project details: {e}")
        await callback_query.answer("Error showing project details")


@dp.callback_query_handler(lambda c: c.data.startswith("back_from_details_"), state="*")
async def back_to_specific_project(callback_query: types.CallbackQuery, state: FSMContext):
    project_id = int(callback_query.data.split("_")[-1])

    await state.update_data(current_project_id=project_id)

    modified_callback = callback_query
    modified_callback.data = "move_0"

    await move_project(modified_callback, state)


# endregion

# region Purchase

@dp.callback_query_handler(lambda c: c.data.startswith("invest_"), state=ProjectCarouselState.current_project_index)
@with_user
async def invest_in_project(user: User, callback_query: types.CallbackQuery, session: Session):
    project_id = int(callback_query.data.split("_")[1])

    project = session.query(Project).filter_by(projectID=project_id).first()
    if not project:
        await callback_query.answer("Project not found...", show_alert=True)
        return

    # Check if this is a child project
    if project.status == "child":
        # Show explanation message about child projects
        await message_manager.send_template(
            user=user,
            template_key='projects/invest/child_project',
            variables={
                'projectName': project.projectName,
                'projectID': project.projectID
            },
            update=callback_query,
            edit=True,
            override_media_id=project.linkImage
        )
        return

    options = session.query(Option).filter_by(
        projectID=project_id,
        isActive=True
    ).all()

    if not options:
        await message_manager.send_template(
            user=user,
            template_key='/projects/invest/noOptions',
            variables={'projectName': project.projectName, 'projectID': project.projectID},
            update=callback_query
        )
        await callback_query.answer("No options available", show_alert=True)
        return

    template_keys = ['/projects/invest']
    template_keys.extend(['/projects/invest/buttons'] * len(options))
    template_keys.append('/projects/invest/buttonBack')

    context = {
        'projectName': project.projectName,
        'projectID': project.projectID,
        'rgroup': {
            'packQty': [opt.packQty for opt in options],
            'packPrice': [opt.packPrice for opt in options]
        },
        'optionID': [opt.optionID for opt in options],
        'packQty': [opt.packQty for opt in options],
        'packPrice': [opt.packPrice for opt in options]
    }

    try:
        await message_manager.send_template(
            user=user,
            template_key=template_keys,
            variables=context,
            update=callback_query,
            edit=True,
            override_media_id=project.linkImage
        )
    except Exception as e:
        logging.error(f"Error showing investment options: {e}")
        await callback_query.answer("Error showing options")


@dp.callback_query_handler(lambda c: c.data.startswith("buy_option_"), state="*")
async def handle_option_selection(callback_query: types.CallbackQuery, state: FSMContext):
    option_id = int(callback_query.data.split("_")[2])

    with Session() as session:
        user = session.query(User).filter_by(telegramID=callback_query.from_user.id).first()
        option = session.query(Option).filter_by(optionID=option_id).first()

        if not user or not option:
            await callback_query.answer("Error...", show_alert=True)
            return

        project = session.query(Project).filter_by(projectID=option.projectID).first()

        if user.balanceActive >= option.packPrice:
            await proceed_to_purchase(callback_query, option, session)
        else:
            await message_manager.send_template(
                user=user,
                template_key='/projects/invest/insufficientFunds',
                variables={
                    'balance': user.balanceActive,
                    'price': option.packPrice,
                    'projectID': option.projectID
                },
                update=callback_query,
                edit=True,
                override_media_id=project.linkImage if project else None
            )


async def proceed_to_purchase(callback_query: types.CallbackQuery, option: Option, session):
    project = session.query(Project).filter_by(projectID=option.projectID).first()

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

        try:
            await message_manager.send_template(
                user=user,
                template_key='/projects/invest/purchaseStart',
                variables={
                    'projectName': project.projectName,
                    'projectID': project.projectID,
                    'packQty': option.packQty,
                    'packPrice': option.packPrice,
                    'optionID': option.optionID
                },
                update=callback_query,
                edit=True,
                override_media_id=project.linkImage if project else None
            )
        except Exception as e:
            logging.error(f"Error showing purchase confirmation: {e}")
            await callback_query.answer("Error showing confirmation")


@dp.callback_query_handler(lambda c: c.data.startswith("confirm_purchase_"), state="*")
async def confirm_purchase(callback_query: types.CallbackQuery):
    option_id = int(callback_query.data.split("_")[2])

    with Session() as session:
        try:
            session.begin_nested()

            # Get user and option with locks
            user = session.query(User).filter_by(
                telegramID=callback_query.from_user.id
            ).with_for_update().first()

            option = session.query(Option).filter_by(
                optionID=option_id
            ).with_for_update().first()

            if not user or not option:
                await callback_query.answer("Error processing purchase", show_alert=True)
                return

            if user.balanceActive < option.packPrice:
                await message_manager.send_template(
                    user=user,
                    template_key='/projects/invest/insufficientFunds',
                    variables={
                        'balance': user.balanceActive,
                        'price': option.packPrice
                    },
                    update=callback_query,
                    edit=True
                )
                return

            project = (session.query(Project)
                       .filter(Project.projectID == option.projectID,
                               Project.lang == user.lang)
                       .first() or
                       session.query(Project)
                       .filter(Project.projectID == option.projectID,
                               Project.lang == 'en')
                       .first())

            purchase = Purchase(
                userID=user.userID,
                projectID=option.projectID,
                projectName=option.projectName,
                optionID=option.optionID,
                packQty=option.packQty,
                packPrice=option.packPrice,
            )

            session.add(purchase)
            session.flush()

            # Создаем запись о списании в ActiveBalance
            active_balance_record = ActiveBalance(
                userID=user.userID,
                firstname=user.firstname,
                surname=user.surname,
                amount=-option.packPrice,  # Отрицательная сумма для списания
                status='done',
                reason=f'purchase={purchase.purchaseID}',  # ID покупки как reason
                link='',  # Пока пустой
                notes='Purchase payment'
            )

            user.balanceActive -= option.packPrice
            session.add(active_balance_record)
            session.commit()

            asyncio.create_task(process_purchase_with_bonuses(purchase.purchaseID))

            try:
                await message_manager.send_template(
                    user=user,
                    template_key='/projects/invest/purchseSuccess',
                    variables={
                        'packQty': int(option.packQty),  # Преобразуем в целое число
                        'packPrice': float(option.packPrice),  # Преобразуем в число с плавающей точкой
                        'balance': float(user.balanceActive)  # Преобразуем в число с плавающей точкой
                    },
                    update=callback_query,
                    edit=True,
                    override_media_id=project.linkImage if project else None
                )
            except Exception as e:
                logging.error(f"Error showing success message: {e}")

        except Exception as e:
            session.rollback()
            logging.error(f"Error processing purchase: {e}")
            await callback_query.answer("Error processing purchase", show_alert=True)


# endregion

# region Add Balance

@dp.callback_query_handler(lambda c: c.data == "add_balance", state="*")
@with_user
async def add_balance_start(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    await state.update_data(db_user_id=user.userID)

    await message_manager.send_template(
        user=user,
        template_key='add_balance_step1',
        update=callback_query,
        delete_original=True
    )

    await PurchaseFlow.waiting_for_payment.set()


@dp.callback_query_handler(lambda c: c.data.startswith("amount_"), state=PurchaseFlow.waiting_for_payment)
async def select_amount(callback_query: types.CallbackQuery, state: FSMContext):
    amount = callback_query.data.split("_")[1]

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

    if amount == "custom":
        await message_manager.send_template(
            user=user,
            template_key='add_balance_custom',
            update=callback_query,
            edit=True
        )
        return

    await state.update_data(amount=float(amount))

    await message_manager.send_template(
        user=user,
        template_key='add_balance_currency',
        update=callback_query,
        variables={'amount': float(amount)},
        edit=True
    )


@dp.message_handler(state=PurchaseFlow.waiting_for_payment, content_types=types.ContentTypes.TEXT)
async def custom_amount_input(message: types.Message, state: FSMContext):
    with Session() as session:
        user, success = await helpers.get_user_from_update(message, session)
        if not success:
            return

        try:
            amount = float(message.text.strip())
            if amount <= 0:
                raise ValueError()

            await state.update_data(amount=amount)

            await message_manager.send_template(
                user=user,
                template_key='add_balance_currency',
                update=message,
                variables={'amount': amount}
            )

        except ValueError:
            await message_manager.send_template(
                user=user,
                template_key='add_balance_amount_error',
                update=message
            )


@dp.callback_query_handler(lambda c: c.data.startswith("currency_"), state=PurchaseFlow.waiting_for_payment)
async def confirm_invoice(callback_query: types.CallbackQuery, state: FSMContext):
    currency = callback_query.data.split("_")[1]
    user_data = await state.get_data()

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

        if currency in config.STABLECOINS:
            currency_rate = 1.0
        else:
            crypto_rates = await GlobalVariables().get('crypto_rates')
            currency_rate = crypto_rates.get(currency)

            if not currency_rate:
                await message_manager.send_template(
                    user=user,
                    template_key='add_balance_rate_error',
                    update=callback_query,
                    variables={'currency': currency},
                    edit=True
                )
                return

        amount_usd = user_data["amount"]
        amount_currency = round(amount_usd / currency_rate, 2)

        await state.update_data(currency=currency, amount_currency=amount_currency)

        await message_manager.send_template(
            user=user,
            template_key='add_balance_confirm',
            update=callback_query,
            variables={
                'amount_usd': amount_usd,
                'currency': currency,
                'amount_currency': amount_currency
            },
            edit=True
        )


async def create_payment(user: User, payment_data: dict) -> dict:
    wallets = await GlobalVariables().get('wallets')
    wallet_address = wallets.get(payment_data["currency"])

    with Session() as session:
        payment = Payment(
            userID=user.userID,
            firstname=user.firstname,
            surname=user.surname,
            direction='incoming',
            amount=payment_data["amount"],
            method=payment_data["currency"],
            fromWallet=None,
            toWallet=wallet_address,
            txid=None,
            sumCurrency=payment_data["amount_currency"],
            status="pending"
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)

        return {
            "amount": payment.amount,
            "method": payment.method,
            "sumCurrency": payment.sumCurrency,
            "toWallet": payment.toWallet,
            "payment_id": payment.paymentID
        }


@dp.callback_query_handler(lambda c: c.data == "confirm_payment", state=PurchaseFlow.waiting_for_payment)
async def create_payment_record(callback_query: types.CallbackQuery, state: FSMContext):
    payment_data = await state.get_data()

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

    try:
        invoice_data = await create_payment(user=user, payment_data=payment_data)
    except Exception as e:
        logging.error(f"Error creating payment: {e}")
        await message_manager.send_template(
            user=user,
            template_key='add_balance_creation_error',
            update=callback_query,
            edit=True
        )
        return

    await message_manager.send_template(
        user=user,
        template_key=['add_balance_created', 'pending_invoice_details'],
        update=callback_query,
        variables={
            'amount': invoice_data['amount'],
            'method': invoice_data['method'],
            'sumCurrency': invoice_data['sumCurrency'],
            'wallet': invoice_data['toWallet'],
            'payment_id': invoice_data['payment_id']
        },
        edit=True
    )
    await state.finish()


@dp.callback_query_handler(lambda c: c.data.startswith("enter_txid_"), state="*")
async def request_txid(callback_query: types.CallbackQuery, state: FSMContext):
    payment_id = int(callback_query.data.split("_")[2])

    with Session() as session:
        user, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

        await state.update_data(payment_id=payment_id)

        await message_manager.send_template(
            user=user,
            template_key='add_balance_enter_txid',
            update=callback_query,
            variables={}
        )
        await TxidInputState.waiting_for_txid.set()


@dp.message_handler(state=TxidInputState.waiting_for_txid, content_types=types.ContentTypes.TEXT)
async def process_txid_input(message: types.Message, state: FSMContext):
    txid = message.text.strip()
    state_data = await state.get_data()
    payment_id = state_data.get('payment_id')

    with Session() as session:
        user, success = await helpers.get_user_from_update(message, session)
        if not success:
            await state.finish()
            return

        # Check payment exists and belongs to user
        payment = session.query(Payment).filter_by(
            paymentID=payment_id,
            status="pending",
            userID=user.userID
        ).first()

        if not payment:
            await message_manager.send_template(
                user=user,
                template_key='txid_payment_not_found',
                update=message,
                variables={}
            )
            await state.finish()
            return

        # Check if TXID is already used
        existing_payment = session.query(Payment).filter_by(txid=txid).first()
        if existing_payment:
            await message_manager.send_template(
                user=user,
                template_key='txid_already_used',
                update=message,
                variables={}
            )
            await state.finish()
            return

        try:
            # Validate TXID format
            validation_result = validate_txid(txid, payment.method)
            if validation_result.code != TxidValidationCode.VALID_TRANSACTION:
                await message_manager.send_template(
                    user=user,
                    template_key=config.TXID_TEMPLATE_MAPPING[validation_result.code],
                    update=message,
                    variables={'details': validation_result.details} if validation_result.details else {}
                )
                return

            # Verify transaction
            verification_result = await verify_transaction(
                txid,
                payment.method,
                payment.toWallet or config.WALLETS.get(payment.method)
            )

            if verification_result.code != TxidValidationCode.VALID_TRANSACTION:
                await message_manager.send_template(
                    user=user,
                    template_key=config.TXID_TEMPLATE_MAPPING[verification_result.code],
                    update=message,
                    variables={
                        'details': verification_result.details,
                        'from_address': verification_result.from_address,
                        'to_address': verification_result.to_address,
                        'expected_address': payment.toWallet or config.WALLETS.get(payment.method)
                    }
                )
                return

            try:
                session.begin_nested()  # Create savepoint

                payment.txid = txid
                payment.status = "check"
                payment.fromWallet = verification_result.from_address

                session.commit()

                try:
                    await create_payment_check_notification(payment, user)
                    template_key = 'txid_success'
                except Exception as e:
                    logging.error(f"Error creating notification for payment {payment.paymentID}: {e}")
                    template_key = 'txid_success_no_notify'

                await message_manager.send_template(
                    user=user,
                    template_key=template_key,
                    update=message,
                    variables={}
                )
                await state.finish()

            except Exception as e:
                session.rollback()
                logging.error(f"Error updating payment {payment_id} with txid {txid}: {e}")

                await message_manager.send_template(
                    user=user,
                    template_key='txid_save_error',
                    update=message,
                    variables={}
                )

        except Exception as e:
            logging.error(f"Error processing TXID {txid}: {e}")

            await message_manager.send_template(
                user=user,
                template_key='txid_error',
                update=message,
                variables={'error': str(e)}
            )


@dp.callback_query_handler(lambda c: c.data == "cancel_payment", state=PurchaseFlow.waiting_for_payment)
async def cancel_payment(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer("Operation cancelled")
    await state.finish()
    await finances(callback_query)


# endregion

# region Уведомления о платежах и их подтверждение

async def create_user_payment_notification(payment: Payment, payer: User, is_approved: bool) -> Notification:
    """Creates notification for user about payment status change."""
    text, buttons = await MessageTemplates.get_raw_template(
        'user_payment_approved' if is_approved else 'user_payment_rejected',
        {
            'payment_id': payment.paymentID,
            'payment_date': payment.createdAt.strftime('%Y-%m-%d %H:%M:%S'),
            'amount': payment.amount,
            'balance': payer.balanceActive,
            'txid': payment.txid
        }
    )

    return Notification(
        source="payment_processor",
        text=text,  # Используем text из кортежа
        buttons=buttons,  # И buttons из кортежа, если они есть в шаблоне
        target_type="user",
        target_value=str(payer.userID),
        priority=2,
        category="payment",
        importance="high",
        parse_mode="HTML"
    )


async def create_payment_check_notification(payment: Payment, user: User) -> None:
    """Creates notification for admins about new payment."""
    with Session() as session:
        if not config.ADMIN_USER_IDS:
            logging.error("No admin users found in database!")
            raise ValueError("No admin users found in database!")

        text, buttons = await MessageTemplates.get_raw_template(
            'admin_new_payment_notification',
            {
                'user_name': user.firstname,
                'user_id': user.userID,
                'payment_id': payment.paymentID,
                'payment_date': payment.createdAt,
                'amount': payment.amount,
                'method': payment.method,
                'sum_currency': payment.sumCurrency,
                'txid': payment.txid,
                'wallet': payment.toWallet,
                'tx_browser_url': config.TX_BROWSERS[payment.method]
            }
        )

        for admin_id in config.ADMIN_USER_IDS:
            notification = Notification(
                source="payment_checker",
                text=text,  # Теперь используем text из кортежа
                buttons=buttons,  # И buttons из кортежа
                target_type="user",
                target_value=str(admin_id),
                priority=2,
                parse_mode="HTML",
                category="payment",
                importance="high"
            )
            session.add(notification)

        session.commit()


@dp.callback_query_handler(lambda c: c.data.startswith('approve_payment_'), state='*')
async def handle_initial_approval(callback_query: types.CallbackQuery):
    """Первый шаг подтверждения платежа"""
    if callback_query.from_user.id not in config.ADMINS:
        await callback_query.answer("Недостаточно прав")
        return

    payment_id = int(callback_query.data.split('_')[-1])

    with Session() as session:
        # Получаем объект админа
        admin, success = await helpers.get_user_from_update(callback_query, session)
        if not success:
            return

        payment = session.query(Payment).filter_by(paymentID=payment_id).first()
        if not payment:
            await callback_query.answer("Платёж не найден")
            return

        # Проверяем, что платёж еще не обработан
        if payment.status != "check":
            text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
                admin,
                'admin_payment_wrong_status',
                {
                    'payment_id': payment_id,
                    'status': payment.status
                }
            )
            await callback_query.message.edit_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                disable_web_page_preview=disable_preview
            )
            return

        text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
            admin,
            'admin_payment_confirm_action',
            {
                'payment_id': payment_id,
                'action': 'подтвердить'
            }
        )
        await callback_query.message.edit_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
            disable_web_page_preview=disable_preview
        )


@dp.callback_query_handler(lambda c: c.data.startswith('final_approve_'), state='*')
async def handle_final_approval(callback_query: types.CallbackQuery):
    """Финальное подтверждение платежа"""
    if callback_query.from_user.id not in config.ADMINS:
        await callback_query.answer("Недостаточно прав")
        return

    payment_id = int(callback_query.data.split('_')[-1])

    with Session() as session:
        try:
            admin, success = await helpers.get_user_from_update(callback_query, session)
            if not success:
                return

            session.begin_nested()

            payment = session.query(Payment).filter_by(
                paymentID=payment_id
            ).with_for_update().first()

            if not payment or payment.status != "check":
                await callback_query.answer("Платёж не найден или уже обработан")
                return

            payer = session.query(User).filter_by(userID=payment.userID).with_for_update().first()
            if not payer:
                raise ValueError(f"Пользователь не найден для платежа {payment_id}")

            # Создаем запись в ActiveBalance
            active_balance_record = ActiveBalance(
                userID=payer.userID,
                firstname=payer.firstname,
                surname=payer.surname,
                amount=payment.amount,
                status='done',
                reason=f'payment={payment_id}',
                link='',
                notes=f'Payment approved by admin: {admin.userID}'
            )
            session.add(active_balance_record)

            payer.balanceActive += payment.amount
            payment.status = "paid"
            payment.confirmedBy = str(callback_query.from_user.id)
            payment.confirmationTime = datetime.utcnow()

            notification = await create_user_payment_notification(payment, payer, is_approved=True)
            session.add(notification)

            text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
                admin,
                'admin_payment_approved',
                {
                    'payment_id': payment_id,
                    'user_name': payer.firstname,
                    'user_id': payer.userID,
                    'amount': payment.amount
                }
            )

            session.commit()

            await callback_query.message.edit_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                disable_web_page_preview=disable_preview
            )

        except Exception as e:
            session.rollback()
            logging.error(f"Error approving payment {payment_id}: {e}")
            await callback_query.answer(f"Ошибка при подтверждении платежа: {str(e)}")


@dp.callback_query_handler(lambda c: c.data.startswith('reject_payment_'), state='*')
async def handle_rejection(callback_query: types.CallbackQuery):
    """Отклонение платежа (работает на любом этапе)"""
    if callback_query.from_user.id not in config.ADMINS:
        await callback_query.answer("Недостаточно прав")
        return

    payment_id = int(callback_query.data.split('_')[-1])

    with Session() as session:
        try:
            admin, success = await helpers.get_user_from_update(callback_query, session)
            if not success:
                return

            session.begin_nested()

            payment = session.query(Payment).filter_by(
                paymentID=payment_id
            ).with_for_update().first()

            if not payment or payment.status not in ["check", "pending"]:
                await callback_query.answer("Платёж не найден или уже обработан")
                return

            payer = session.query(User).filter_by(userID=payment.userID).with_for_update().first()
            if not payer:
                raise ValueError(f"Пользователь не найден для платежа {payment_id}")

            payment.status = "failed"
            payment.confirmedBy = str(callback_query.from_user.id)
            payment.confirmationTime = datetime.utcnow()

            notification = await create_user_payment_notification(payment, payer, is_approved=False)
            session.add(notification)

            text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
                admin,
                'admin_payment_rejected',
                {
                    'payment_id': payment_id,
                    'user_name': payer.firstname,
                    'user_id': payer.userID
                }
            )

            session.commit()

            await callback_query.message.edit_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                disable_web_page_preview=disable_preview
            )

        except Exception as e:
            session.rollback()
            logging.error(f"Error rejecting payment {payment_id}: {e}")
            await callback_query.answer(f"Ошибка при отклонении платежа: {str(e)}")


# endregion

# region хендлер TEAM

@dp.callback_query_handler(lambda c: c.data == "/team", state="*")
@with_user
async def handle_team(user: User, callback_query: types.CallbackQuery, session: Session):
    upline_count = session.query(func.count(User.userID)).filter(
        User.upline == user.telegramID
    ).scalar() or 0

    def get_all_referrals(telegram_id, visited=None):
        if visited is None:
            visited = set()

        referrals = session.query(User.telegramID).filter(
            User.upline == telegram_id
        ).all()

        total = 0
        for (ref_id,) in referrals:
            if ref_id not in visited:
                visited.add(ref_id)
                total += 1 + get_all_referrals(ref_id, visited)
        return total

    upline_total = get_all_referrals(user.telegramID)

    await message_manager.send_template(
        user=user,
        template_key='/team',
        update=callback_query,
        variables={
            'userInvitedUplineFirst': upline_count,
            'userInvitedUplineTotal': upline_total
        },
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/team/referal/info", state="*")
@with_user
async def start_referral_link_dialog(user: User, callback_query: types.CallbackQuery, session: Session):
    await message_manager.send_template(
        user=user,
        template_key='/team/referal/info',
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/team/referal/card", state="*")
@with_user
async def show_referral_link(user: User, callback_query: types.CallbackQuery, session: Session):
    ref_link = f"<a href='https://t.me/{BOT_USERNAME}?start={user.telegramID}'>🚀JETUP!🚀</a>"

    # Показываем экран с ссылкой
    await message_manager.send_template(
        user=user,
        template_key='/team/referal/card',
        variables={
            'ref_link': ref_link,
            'firstname': user.firstname,
            'user_id': user.userID
        },
        update=callback_query,
        edit=True
    )


@dp.callback_query_handler(lambda c: c.data == "/team/marketing", state="*")
@with_user
async def show_marketing_info(user: User, callback_query: types.CallbackQuery, session: Session):
    bonus_levels = []
    levels = []
    percents = []

    for key, value in config.PURCHASE_BONUSES.items():
        if key.startswith("level_"):
            level_num = int(key.split("_")[1])
            bonus_levels.append((level_num, value))

    bonus_levels.sort()

    for level, percent in bonus_levels:
        levels.append(level)
        percents.append(percent)

    await message_manager.send_template(
        user=user,
        template_key='/team/marketing',
        variables={
            'rgroup': {
                'level': levels,
                'percent': percents
            }
        },
        update=callback_query,
        delete_original=True
    )


@dp.callback_query_handler(lambda c: c.data == "/team/stats", state="*")
@with_user
async def handle_team_stats(user: User, callback_query: types.CallbackQuery, session: Session):
    await helpers.safe_delete_message(callback_query)

    ref_link = f"https://t.me/{BOT_USERNAME}?start={user.telegramID}"

    now = datetime.utcnow()
    current_month_start = datetime(now.year, now.month, 1)

    logging.info(f"Calculating stats from {current_month_start}")

    referrals = session.query(User).filter(
        User.upline == user.telegramID
    ).all()

    total_referrals = len(referrals)
    new_referrals = len([r for r in referrals if r.createdAt and r.createdAt >= current_month_start])

    referral_ids = [r.userID for r in referrals]

    total_purchases = Decimal('0')
    monthly_purchases = Decimal('0')

    if referral_ids:
        referral_purchases = session.query(Purchase).filter(
            Purchase.userID.in_(referral_ids)
        ).order_by(Purchase.createdAt).all()

        logging.info(f"Found purchases for referrals {referral_ids}:")
        for purchase in referral_purchases:
            amount = Decimal(str(purchase.packPrice))  # Заменяем amount на packPrice
            total_purchases += amount
            if purchase.createdAt and purchase.createdAt >= current_month_start:
                monthly_purchases += amount
                logging.info(f"Monthly purchase - ID: {purchase.purchaseID}, User: {purchase.userID}, "
                             f"Date: {purchase.createdAt}, Amount: {amount}")
            else:
                logging.info(f"Earlier purchase - ID: {purchase.purchaseID}, User: {purchase.userID}, "
                             f"Date: {purchase.createdAt}, Amount: {amount}")

        logging.info(f"Total purchases sum: {total_purchases}")
        logging.info(f"Current month purchases sum: {monthly_purchases}")

    await message_manager.send_template(
        user=user,
        template_key='/team/stats',
        update=callback_query,
        variables={
            'ref_link': ref_link,
            'total_refs': total_referrals,
            'new_refs': new_referrals,
            'total_purchases': float(total_purchases),
            'monthly_purchases': float(monthly_purchases)
        }
    )


# endregion

# region хендлер SETTINGS

async def get_settings_template_keys(user: User) -> list:
    """Возвращает список ключей шаблонов для экрана настроек на основе данных пользователя"""
    template_keys = ['settings_main']

    if not user.isFilled:
        template_keys.append('settings_unfilled_data')
    elif user.isFilled and not helpers.is_email_confirmed(user):
        template_keys.append('settings_filled_unconfirmed')

    template_keys.append('settings_language')

    return template_keys


@dp.callback_query_handler(lambda c: c.data == "settings", state="*")
@with_user
async def handle_settings(user: User, callback_query: types.CallbackQuery, session: Session):
    """Shows settings screen with language selection and user data status"""
    await helpers.safe_delete_message(callback_query)

    template_keys = await get_settings_template_keys(user)

    await message_manager.send_template(
        user=user,
        template_key=template_keys,
        variables={'current_lang': user.lang or 'en'},
        update=callback_query
    )


@dp.callback_query_handler(lambda c: c.data.startswith('settings_lang_'), state="*")
@with_user
async def handle_settings_language_select(user: User, callback_query: types.CallbackQuery, session: Session):
    """Handles language selection in settings"""
    lang = callback_query.data.split('_')[2]

    if user.lang == lang:
        return

    user.lang = lang
    session.commit()

    template_keys = await get_settings_template_keys(user)

    await message_manager.send_template(
        user=user,
        template_key=template_keys,
        variables={'current_lang': lang},
        update=callback_query,
        edit=True
    )


# endregion

# region хендлер HELP

INFO_SCREENS = {
    "/help": {
        "template_key": "/help",
        "variables": lambda: {"faq_url": GlobalVariables()._variables.get('faq_url', "")}  # Прямой доступ к кэшу
    },
    "/help/contacts": {
        "template_key": "/help/contacts",
        "variables": lambda: {"rgroup": {"admin_link": config.ADMIN_LINKS}}
    },
    "/help/social": {
        "template_key": "/help/social",
        "variables": lambda: GlobalVariables()._variables.get('social_links', {})
    }
}


@dp.callback_query_handler(lambda c: c.data in INFO_SCREENS, state="*")
@with_user
async def handle_info_screen(user: User, callback_query: types.CallbackQuery, session: Session):
    callback_data = callback_query.data
    screen_config = INFO_SCREENS[callback_data]

    template_key = screen_config["template_key"]
    variables = screen_config["variables"]()

    await message_manager.send_template(
        user=user,
        template_key=template_key,
        variables=variables,
        update=callback_query,
        delete_original=True
    )


# endregion

# region Misc

@dp.callback_query_handler(lambda c: "/download/csv/" in c.data, state="*")
@with_user
async def handle_csv_download(user: User, callback_query: types.CallbackQuery, session: Session):
    """Universal handler for CSV report downloads
        Extract report type from callback_data
        Format: /something/download/csv/report_name"""

    callback_data = callback_query.data
    parts = callback_data.split("/download/csv/")
    if len(parts) != 2:
        logger.error(f"Invalid callback format: {callback_data}")
        return

    report_type = parts[1]
    back_button = parts[0]

    # Import csv_reports here to avoid circular imports
    from csv_reports import generate_csv_report, REPORTS

    try:
        await message_manager.send_template(
            user=user,
            template_key='/download/csv/report_generating',
            update=callback_query
        )

        csv_data = generate_csv_report(session, user, report_type)

        if not csv_data:
            await message_manager.send_template(
                user=user,
                template_key='/download/csv/report_error',
                variables={'back_button': back_button or '/dashboard/existingUser'},
                update=callback_query
            )
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_name = REPORTS.get(report_type, {}).get("name", report_type.capitalize())
        filename = f"{report_name}_{timestamp}.csv"

        document = types.InputFile(csv_data, filename=filename)
        await callback_query.message.answer_document(document=document)

        await message_manager.send_template(
            user=user,
            template_key='/download/csv/report_ready',
            variables={'back_button': back_button or '/dashboard/existingUser'},
            update=callback_query
        )

    except Exception as e:
        logging.error(f"Error generating CSV report: {e}", exc_info=True)
        await message_manager.send_template(
            user=user,
            template_key='report_generation_error',
            update=callback_query
        )


@dp.message_handler(content_types=['document', 'photo', 'video', 'sticker'], state='*')
async def handle_admin_file(message: types.Message):
    if message.from_user.id not in config.ADMINS:
        return

    try:
        file_info = None

        if message.document:
            file_id = message.document.file_id
            mime_type = message.document.mime_type
            file_name = message.document.file_name
            file_size = message.document.file_size
            file_info = (f"📄 Document Info:\n"
                         f"File name: {file_name}\n"
                         f"MIME type: {mime_type}\n"
                         f"Size: {file_size} bytes\n"
                         f"<code>{file_id}</code>")

        elif message.photo:
            photo = message.photo[-1]
            file_id = photo.file_id
            file_info = (f"🖼 Photo Info:\n"
                         f"Width: {photo.width}px\n"
                         f"Height: {photo.height}px\n"
                         f"Size: {photo.file_size} bytes\n"
                         f"<code>{file_id}</code>")

        elif message.video:
            video = message.video
            file_id = video.file_id
            file_info = (f"🎥 Video Info:\n"
                         f"Duration: {video.duration} seconds\n"
                         f"Width: {video.width}px\n"
                         f"Height: {video.height}px\n"
                         f"Size: {video.file_size} bytes\n"
                         f"<code>{file_id}</code>")

        elif message.sticker:
            sticker = message.sticker
            file_info = (f"🎯 Sticker Info:\n"
                         f"Set name: {sticker.set_name}\n"
                         f"Emoji: {sticker.emoji}\n"
                         f"Width: {sticker.width}px\n"
                         f"Height: {sticker.height}px\n"
                         f"Is animated: {'Yes' if sticker.is_animated else 'No'}\n"
                         f"Is video: {'Yes' if sticker.is_video else 'No'}\n"
                         f"<code>{sticker.file_id}</code>")

        if file_info:
            await message.reply(file_info, parse_mode="HTML")

    except Exception as e:
        error_msg = f"❌ Произошла ошибка при обработке файла: {str(e)}"
        await message.reply(error_msg)
        logging.error(error_msg)


@dp.callback_query_handler(lambda c: c.data.startswith("download_pdf_"),
                           state=ProjectCarouselState.current_project_index)
@with_user
async def download_project_pdf(user: User, callback_query: types.CallbackQuery, session: Session):
    logger.info(f"Processing PDF download request: {callback_query.data}")

    try:
        # Разбираем callback_data
        callback_parts = callback_query.data.split("_")
        if len(callback_parts) < 3:
            logger.warning(f"Invalid callback format: {callback_query.data}")
            await callback_query.answer("Invalid request format!")
            return

        project_doc = "_".join(callback_parts[2:])
        logger.debug(f"Extracted project_doc: {project_doc}")

        if "~" in project_doc:
            project_id_str, doc_id = project_doc.split("~", 1)
            project_id = int(project_id_str)
            logger.debug(f"Parsed project_id: {project_id}, doc_id: {doc_id}")
        else:
            project_id = int(project_doc)
            doc_id = None
            logger.debug(f"Parsed project_id: {project_id}, no doc_id")

        # Получаем проект
        project = session.query(Project).filter(Project.projectID == project_id, Project.lang == user.lang).first()
        if not project:
            project = session.query(Project).filter(Project.projectID == project_id, Project.lang == 'en').first()

        if not project:
            logger.warning(f"Project {project_id} not found")
            await callback_query.answer("Project not found!")
            return

        if not project.linkPres:
            logger.warning(f"Project {project_id} has no linkPres data")
            await callback_query.answer("No documents available!")
            return

        logger.info(f"Raw linkPres data: {repr(project.linkPres)}")

        # Парсим ссылки
        link_pres = {}
        try:
            if ": " not in project.linkPres:
                link_pres["default"] = project.linkPres.strip()
                logger.debug(f"Single link format, using default: {link_pres['default']}")
            else:
                cleaned_link_pres = project.linkPres.replace(",\n", ",").replace(", ", ",")
                pairs = [pair.strip() for pair in cleaned_link_pres.split(",") if pair.strip()]
                logger.debug(f"Found {len(pairs)} pairs to parse: {pairs}")

                for i, pair in enumerate(pairs):
                    if ": " in pair:
                        key, value = pair.split(": ", 1)
                        key, value = key.strip(), value.strip()
                        link_pres[key] = value
                        logger.debug(f"Pair {i}: '{key}' -> '{value}'")
                    else:
                        link_pres["default"] = pair.strip()
                        logger.debug(f"Pair {i}: default -> '{pair.strip()}'")

            logger.info(f"Parsed link_pres dictionary: {link_pres}")

            # Определяем какую ссылку использовать
            selected_key = None
            file_identifier = None

            if doc_id and doc_id in link_pres:
                selected_key = doc_id
                file_identifier = link_pres[doc_id]
            elif link_pres:
                selected_key = next(iter(link_pres))
                file_identifier = link_pres[selected_key]
            else:
                logger.error("No valid links found in parsed data")
                await callback_query.answer("No documents found!")
                return

            logger.info(f"Selected key: '{selected_key}', file_identifier: '{file_identifier}'")

            # Проверяем, что это похоже на Telegram file_id
            def is_telegram_file_id(identifier):
                # Telegram file_id обычно содержат только буквы, цифры, дефисы и подчеркивания
                # и не содержат пробелы, кириллицу или другие специальные символы
                if not identifier:
                    return False
                # Простая проверка на формат
                allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_')
                return all(c in allowed_chars for c in identifier) and len(identifier) > 10

            if not is_telegram_file_id(file_identifier):
                logger.error(f"File identifier '{file_identifier}' doesn't look like a valid Telegram file_id")
                logger.error(f"File identifier length: {len(file_identifier)}")
                logger.error(f"File identifier characters: {[ord(c) for c in file_identifier[:20]]}")
                await callback_query.answer("Document format not supported. Please contact admin.")
                return

            # Отправляем документ
            logger.info(f"Attempting to send document with file_id: {file_identifier}")
            await callback_query.answer("Downloading PDF...")

            try:
                await bot.send_document(
                    chat_id=callback_query.message.chat.id,
                    document=file_identifier,
                    caption=f"Document: {selected_key}" if selected_key != "default" else None
                )
                logger.info(f"Successfully sent document {file_identifier}")
            except Exception as send_error:
                logger.error(f"Failed to send document {file_identifier}: {send_error}")
                await callback_query.message.answer(
                    f"❌ Failed to send document. Error: {str(send_error)}\n"
                    f"File ID: {file_identifier[:50]}..."
                )

        except ValueError as parse_error:
            logger.error(f"Failed to parse linkPres data: {parse_error}")
            logger.error(f"Raw linkPres: {repr(project.linkPres)}")
            await callback_query.answer("Error processing document data!")

    except Exception as e:
        logger.error(f"Unexpected error in download_project_pdf: {e}", exc_info=True)
        logger.error(f"Callback data: {callback_query.data}")
        logger.error(f"User: {user.userID}")
        await callback_query.answer("An error occurred while processing your request!")


@dp.callback_query_handler(lambda c: c.data == "/dashboard/existingUser", state="*")
async def back_to_start(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.message:
        await helpers.safe_delete_message(callback_query)

    fake_message = helpers.FakeMessage(
        from_user=callback_query.from_user,
        chat=callback_query.message.chat,
        bot=callback_query.bot,
        args=''
    )

    await send_welcome(fake_message, state)


@dp.callback_query_handler(state="*", run_task=True)
@with_user
async def handle_unknown_callback(user: User, callback_query: types.CallbackQuery, session: Session, state: FSMContext):
    logging.warning(f"Unhandled callback data: {callback_query.data}")

    await message_manager.send_template(
        user=user,
        template_key='/fallback',  # Специальный шаблон-заглушка
        update=callback_query,
        variables={'callback_data': callback_query.data},  # Передаём data для отладки
    )

    await callback_query.answer("TEMPORARY DISABLED!", show_alert=False)


# endregion

# region Служебные функции
async def get_bot_username():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Bot username: {BOT_USERNAME}")


async def start_bot():
    await get_bot_username()
    while True:
        try:
            await dp.start_polling()
        except (NetworkError, TelegramAPIError) as e:
            logger.error(f"Ошибка соединения: {e}. Перезапуск через 5 секунд...")
            await asyncio.sleep(5)


async def setup():
    logger.info("Starting application setup...")

    Session, engine = get_session()
    init_tables(engine)
    logger.info("Database initialized")

    await MessageTemplates.load_templates()
    logger.info("Message templates loaded")

    # Load configuration from Google Sheets ПЕРЕД инициализацией переменных!
    logger.info("Loading configuration from Google Sheets...")
    try:
        from imports import ConfigImporter
        config_dict = await ConfigImporter.import_config()
        ConfigImporter.update_config_module(config_dict)
        logger.info(f"Loaded {len(config_dict)} config variables from Google Sheets")
    except Exception as e:
        logger.error(f"Failed to load config from Google Sheets: {e}")

    # Теперь инициализируем переменные с обновленными значениями
    global_vars = initialize_variables()
    global_vars.set_static_variable('Session', Session)
    global_vars.set_static_variable('message_manager', message_manager)

    # Пересоздаем email_manager с новыми настройками
    import email_sender
    email_sender.email_manager = email_sender.EmailManager()
    logger.info("EmailManager reinitialized with updated config")

    bookstack_manager = BookStackManager()
    if bookstack_manager.is_available():
        logger.info("BookStack клиент инициализирован")
    else:
        logger.warning("BookStack недоступен, будет использоваться запасной метод")

    with Session() as session:
        admin_user_ids = [
            user.userID for user in session.query(User).filter(User.telegramID.in_(config.ADMINS)).all()
        ]
        if not admin_user_ids:
            logger.warning("No admin user IDs found in database!")

    config.ADMIN_USER_IDS = admin_user_ids
    logger.info(f"Admin user IDs configured: {admin_user_ids}")

    return global_vars


async def start_services(global_vars):
    """Запуск вспомогательных сервисов"""
    services = []

    services.append(asyncio.create_task(
        global_vars.start_update_loop(),
        name="global_variables"
    ))

    notification_processor = NotificationProcessor()
    services.append(asyncio.create_task(
        notification_processor.run(),
        name="notifications"
    ))

    invoice_cleaner = InvoiceCleaner(bot_username=BOT_USERNAME)
    services.append(asyncio.create_task(
        invoice_cleaner.run(),
        name="invoice_cleaner"
    ))

    sheets_exporter = SheetsExporter()
    services.append(asyncio.create_task(
        sheets_exporter.start(),
        name="sheets_exporter"
    ))

    # Добавляем legacy процессор
    services.append(asyncio.create_task(
        legacy_processor.start(),
        name="legacy_migration"
    ))

    return services


async def main():
    """Основная асинхронная функция"""
    try:
        global_vars = await setup()
        setup_admin_commands(dp, message_manager)
        await get_bot_username()
        services = await start_services(global_vars)
        logger.info("Application setup completed")

        await start_bot()

    except Exception as e:
        logger.error(f"Critical error in main: {e}")
        raise
    finally:
        for task in services:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == '__main__':
    try:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    except Exception as e:
        logger.critical(f"Unexpected error: {e}")
        raise

# endregion
