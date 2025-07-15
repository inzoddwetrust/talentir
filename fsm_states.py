from aiogram.dispatcher.filters.state import State, StatesGroup


class UserDataDialog(StatesGroup):
    waiting_for_firstname = State()
    waiting_for_surname = State()
    waiting_for_birthday = State()
    waiting_for_passport = State()  # New state
    waiting_for_country = State()
    waiting_for_city = State()
    waiting_for_address = State()
    waiting_for_phone = State()
    waiting_for_email = State()
    waiting_for_confirmation = State()


class ProjectCarouselState(StatesGroup):
    wait_for_welcome = State()
    current_project_index = State()
    view_project_details = State()


class PurchaseFlow(StatesGroup):
    waiting_for_payment = State()
    waiting_for_purchase_confirmation = State()


class TxidInputState(StatesGroup):
    waiting_for_txid = State()


class TransferDialog(StatesGroup):
    """Состояния для диалога перевода средств"""
    select_source = State()             # Выбор источника средств (активный/пассивный баланс)
    select_recipient_type = State()     # Выбор типа получателя для пассивного баланса (себе/другому)
    enter_recipient_id = State()        # Ввод ID получателя
    enter_amount = State()              # Ввод суммы перевода
    confirm_transfer = State()          # Подтверждение перевода