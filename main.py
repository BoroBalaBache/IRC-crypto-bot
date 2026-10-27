import asyncio
import os
import time
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest


# =====================================
# initial config
# =====================================

# Load the bot token from the TELEGRAM_BOT_TOKEN environment variable
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

CRYPTO_CONFIG = {
    "TRX": {
        "name": "ترون",
        "symbol": "TRX",
        "networks": ["TRON (TRC20)"],
        "min_amount": Decimal("3"),
        "max_amount": Decimal("50"),
        "price_toman": Decimal("10000"),
        "decimals": 6,
        "enabled": True,
    },
    "BTC": {
        "name": "بیت‌کوین",
        "symbol": "BTC",
        "networks": ["Bitcoin"],
        "min_amount": Decimal("0.0001"),
        "max_amount": Decimal("0.01"),
        "price_toman": Decimal("6000000000"),
        "decimals": 8,
        "enabled": True,
    },
    "ETH": {
        "name": "اتریوم",
        "symbol": "ETH",
        "networks": ["Ethereum (ERC20)"],
        "min_amount": Decimal("0.001"),
        "max_amount": Decimal("1"),
        "price_toman": Decimal("200000000"),
        "decimals": 8,
        "enabled": True,
    },
    "USDT": {
        "name": "تتر",
        "symbol": "USDT",
        "networks": ["TRON (TRC20)", "Ethereum (ERC20)"],
        "min_amount": Decimal("2"),
        "max_amount": Decimal("200"),
        "price_toman": Decimal("100000"),
        "decimals": 6,
        "enabled": True,
    },
}

INVOICE_LIFETIME_SECONDS = 60 # Invoice expiration time: 1 minute
COUNTDOWN_UPDATE_SECONDS = 3  # Update the countdown every 5 seconds

FEE_PERCENT = Decimal("1")
FIXED_FEE_TOMAN = Decimal("10000")

dp = Dispatcher()

# order storag
orders = {}
countdown_tasks = {}


# =====================================
# Conversation states
# =====================================

class BuyState(StatesGroup):
    waiting_for_contact = State()
    waiting_for_crypto = State()
    waiting_for_amount = State()
    waiting_for_network = State()
    waiting_for_address = State()


# =====================================
# Keyboard layouts
# =====================================

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 خرید رمزارز")]],
        resize_keyboard=True,
    )


def contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="📱 تأیید شماره موبایل",
                request_contact=True,
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def crypto_menu():
    buttons = []
    for symbol, config in CRYPTO_CONFIG.items():
        if config["enabled"]:
            buttons.append([
                KeyboardButton(text=f"{config['name']} ({symbol})")
            ])

    buttons.append([KeyboardButton(text="🔙 مرحله قبل")])
    buttons.append([KeyboardButton(text="❌ لغو خرید")])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def navigation_keyboard(show_back=True):
    buttons = []
    if show_back:
        buttons.append([KeyboardButton(text="🔙 مرحله قبل")])
    buttons.append([KeyboardButton(text="❌ لغو خرید")])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def network_menu(symbol):
    config = CRYPTO_CONFIG.get(symbol)
    if not config:
        return None

    buttons = []
    for index, network in enumerate(config["networks"]):
        buttons.append([
            InlineKeyboardButton(
                text=network,
                callback_data=f"network_{symbol}_{index}",
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def invoice_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ تأیید پیش‌فاکتور",
                callback_data=f"invoice_confirm:{order_id}",
            )],
            [InlineKeyboardButton(
                text="❌ لغو سفارش",
                callback_data=f"invoice_cancel:{order_id}",
            )],
        ]
    )


def expired_invoice_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 به‌روزرسانی قیمت و صدور پیش‌فاکتور جدید",
                callback_data=f"invoice_refresh:{order_id}",
            )],
            [InlineKeyboardButton(
                text="❌ لغو سفارش",
                callback_data=f"invoice_cancel:{order_id}",
            )],
        ]
    )


# =====================================
# calculations functions
# =====================================

def format_toman(value):
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.0f}"


def create_invoice(
    amount: Decimal,
    address: str,
    user_id: int,
    symbol: str,
    network: str,
):
    config = CRYPTO_CONFIG.get(symbol)
    if not config or not config["enabled"]:
        raise ValueError("رمزارز انتخاب‌شده معتبر یا فعال نیست.")
    if network not in config["networks"]:
        raise ValueError("شبکه انتخاب‌شده معتبر نیست.")

    price = config["price_toman"]
    subtotal = amount * price
    percentage_fee = (
        subtotal * FEE_PERCENT / Decimal("100")
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    fixed_fee = FIXED_FEE_TOMAN
    total = subtotal + percentage_fee + fixed_fee

    now = time.time()
    order = {
        "order_id": secrets.token_hex(4).upper(),
        "user_id": user_id,
        "crypto": symbol,
        "network": network,
        "amount": amount,
        "address": address,
        "price": price,
        "subtotal": subtotal,
        "percentage_fee": percentage_fee,
        "fixed_fee": fixed_fee,
        "total": total,
        "created_at": now,
        "expires_at": now + INVOICE_LIFETIME_SECONDS,
        "status": "awaiting_confirmation",
    }
    orders[order["order_id"]] = order
    return order


def invoice_text(order):
    remaining = max(0, int(order["expires_at"] - time.time()))
    minutes, seconds = divmod(remaining, 60)
    symbol = order["crypto"]
    config = CRYPTO_CONFIG[symbol]
    decimals = config["decimals"]

    return (
        "🧾 پیش‌فاکتور آزمایشی خرید رمزارز\n\n"
        f"شماره سفارش: {order['order_id']}\n"
        f"رمزارز: {config['name']} ({symbol})\n"
        f"مقدار خرید: {order['amount']:,.{decimals}f} {symbol}\n"
        f"قیمت هر {symbol}: {format_toman(order['price'])} تومان\n"
        f"مبلغ رمزارز: {format_toman(order['subtotal'])} تومان\n"
        f"کارمزد کل: {format_toman(order['percentage_fee'] + order['fixed_fee'])} تومان\n"
        f"مبلغ قابل پرداخت: {format_toman(order['total'])} تومان\n\n"
        f"شبکه مقصد: {order['network']}\n"
        f"آدرس کیف پول: {order['address']}\n\n"
        f"⏳ زمان باقی‌مانده: {minutes:02d}:{seconds:02d}\n\n"
        "⚠️ قیمت و کارمزد آزمایشی هستند؛ این سفارش پرداخت واقعی ندارد."
    )


# =====================================
# Invoice countdown timer
# =====================================

async def run_countdown(bot, order_id):
    try:
        while True:
            order = orders.get(order_id)
            if not order or order["status"] != "awaiting_confirmation":
                return

            remaining = order["expires_at"] - time.time()
            if remaining <= 0:
                order["status"] = "expired"
                try:
                    await bot.edit_message_text(
                        chat_id=order["chat_id"],
                        message_id=order["message_id"],
                        text=(
                            "⏰ این پیش‌فاکتور منقضی شده است.\n\n"
                            f"شماره سفارش: {order_id}\n"
                            "برای ادامه، قیمت را به‌روزرسانی کنید "
                            "تا پیش‌فاکتور جدید صادر شود."
                        ),
                        reply_markup=expired_invoice_keyboard(order_id),
                    )
                except TelegramBadRequest:
                    pass
                return

            try:
                await bot.edit_message_text(
                    chat_id=order["chat_id"],
                    message_id=order["message_id"],
                    text=invoice_text(order),
                    reply_markup=invoice_keyboard(order_id),
                )
            except TelegramBadRequest:
                # خطای ویرایش پیام در شمارشگر مانع ادامه کار نمی‌شود.
                pass

            await asyncio.sleep(COUNTDOWN_UPDATE_SECONDS)

    except asyncio.CancelledError:
        pass
    finally:
        if countdown_tasks.get(order_id) is asyncio.current_task():
            countdown_tasks.pop(order_id, None)


async def send_invoice(message: Message, order):
    sent = await message.answer(
        invoice_text(order),
        reply_markup=invoice_keyboard(order["order_id"]),
    )
    order["chat_id"] = sent.chat.id
    order["message_id"] = sent.message_id

    countdown_tasks[order["order_id"]] = asyncio.create_task(
        run_countdown(message.bot, order["order_id"])
    )


def stop_countdown(order_id):
    task = countdown_tasks.pop(order_id, None)
    if task and not task.done():
        task.cancel()


# =====================================
# Start the bot and collect the user's phone number
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BuyState.waiting_for_contact)
    await message.answer(
        "به ایران اکسچنج خوش آمدید!\n\n"
        "برای ادامه، شماره موبایلی که با آن در ایران اکسچنج "
        "ثبت‌نام کرده‌اید را ارسال کنید.",
        reply_markup=contact_keyboard(),
    )


@dp.message(BuyState.waiting_for_contact, F.contact)
async def contact_handler(message: Message, state: FSMContext):
    contact = message.contact

    if contact.user_id is not None and contact.user_id != message.from_user.id:
        await message.answer("لطفاً شماره موبایل خودتان را ارسال کنید.")
        return

    await state.update_data(phone=contact.phone_number)
    await state.set_state(None)
    await message.answer(
        "✅ شماره موبایل دریافت شد.\n\n"
        "برای شروع فرایند خرید، از منوی زیر «خرید رمزارز» را انتخاب کن.",
        reply_markup=main_menu(),
    )


@dp.message(BuyState.waiting_for_contact)
async def require_contact(message: Message):
    await message.answer(
        "برای ادامه، لطفاً شماره موبایل خودتان را با دکمه زیر ارسال کنید.",
        reply_markup=contact_keyboard(),
    )


# =====================================
# Main menu, purchase cancellation, and navigation
# =====================================

@dp.message(F.text == "❌ لغو خرید")
async def cancel_purchase_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "خرید لغو شد. هر زمان خواستی می‌تونی دوباره شروع کنی.",
        reply_markup=main_menu(),
    )


@dp.message(F.text == "🛒 خرید رمزارز")
async def show_crypto_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("phone"):
        await state.clear()
        await state.set_state(BuyState.waiting_for_contact)
        await message.answer(
            "برای شروع خرید، ابتدا شماره موبایل خودت را ارسال کن.",
            reply_markup=contact_keyboard(),
        )
        return

    await state.update_data(
        crypto=None, amount=None, network=None, address=None
    )
    await state.set_state(BuyState.waiting_for_crypto)
    await message.answer(
        "رمزارز موردنظرت رو انتخاب کن:",
        reply_markup=crypto_menu(),
    )


@dp.message(F.text == "🔙 مرحله قبل")
async def go_back(message: Message, state: FSMContext):
    current = await state.get_state()
    data = await state.get_data()

    if current == BuyState.waiting_for_crypto.state:
        await state.set_state(None)
        await message.answer("به منوی اصلی برگشتی.", reply_markup=main_menu())
        return

    if current == BuyState.waiting_for_amount.state:
        await state.update_data(crypto=None, amount=None, network=None, address=None)
        await state.set_state(BuyState.waiting_for_crypto)
        await message.answer(
            "یک رمزارز انتخاب کن:",
            reply_markup=crypto_menu(),
        )
        return

    if current == BuyState.waiting_for_network.state:
        symbol = data.get("crypto")
        config = CRYPTO_CONFIG.get(symbol)
        if not config:
            await state.clear()
            await message.answer("اطلاعات خرید پیدا نشد. از منوی خرید دوباره شروع کن.", reply_markup=main_menu())
            return

        await state.update_data(network=None, address=None)
        await state.set_state(BuyState.waiting_for_amount)
        await message.answer(
            f"مقدار {symbol} موردنظرت رو وارد کن:",
            reply_markup=navigation_keyboard(),
        )
        return

    if current == BuyState.waiting_for_address.state:
        symbol = data.get("crypto")
        if not symbol or symbol not in CRYPTO_CONFIG:
            await state.clear()
            await message.answer("اطلاعات خرید پیدا نشد. از منوی خرید دوباره شروع کن.", reply_markup=main_menu())
            return

        await state.update_data(network=None, address=None)
        await state.set_state(BuyState.waiting_for_network)
        await message.answer(
            f"رمزارز: {symbol}\n"
            f"مقدار خرید: {data.get('amount')} {symbol}\n\n"
            "شبکه انتقال مقصد را دوباره انتخاب کن:",
            reply_markup=navigation_keyboard(),
        )
        await message.answer(
            "شبکه انتقال مقصد را انتخاب کن:",
            reply_markup=network_menu(symbol),
        )
        return

    await message.answer("در حال حاضر مرحله‌ای برای برگشت وجود ندارد.", reply_markup=main_menu())


# =====================================
# Handle cryptocurrency selection
# =====================================

@dp.message(BuyState.waiting_for_crypto)
async def select_crypto(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("لطفاً یکی از رمزارزهای منو رو انتخاب کن.")
        return

    selected = message.text.strip()
    symbol = next(
        (
            key for key, config in CRYPTO_CONFIG.items()
            if selected == f"{config['name']} ({key})"
        ),
        None,
    )

    if not symbol:
        await message.answer("لطفاً یکی از رمزارزهای منو رو انتخاب کن.")
        return

    config = CRYPTO_CONFIG[symbol]
    if not config["enabled"]:
        await message.answer("این رمزارز در حال حاضر قابل خرید نیست.")
        return

    await state.update_data(crypto=symbol, amount=None, network=None, address=None)
    await state.set_state(BuyState.waiting_for_amount)
    await message.answer(
        f"رمزارز انتخاب‌شده: {config['name']} ({symbol})\n\n"
        f"حداقل خرید: {config['min_amount']} {symbol}\n"
        f"حداکثر خرید: {config['max_amount']} {symbol}\n\n"
        f"مقدار {symbol} موردنظرت رو وارد کن.",
        reply_markup=navigation_keyboard(),
    )


# =====================================
# Handle purchase amount input
# =====================================

@dp.message(BuyState.waiting_for_amount)
async def receive_amount(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("لطفاً مقدار خرید را به‌صورت عددی وارد کن.")
        return

    raw = message.text.strip().replace(",", "").replace("٬", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        await message.answer("مقدار معتبر نیست. مثلاً 0.5 وارد کن.")
        return

    if not amount.is_finite() or amount <= 0:
        await message.answer("مقدار باید عددی بزرگ‌تر از صفر باشد.")
        return

    data = await state.get_data()
    symbol = data.get("crypto")
    config = CRYPTO_CONFIG.get(symbol)

    if not config or not config["enabled"]:
        await state.clear()
        await message.answer(
            "اطلاعات رمزارز پیدا نشد یا خرید آن غیرفعال است. دوباره شروع کن.",
            reply_markup=main_menu(),
        )
        return

    if amount < config["min_amount"]:
        await message.answer(f"حداقل خرید {symbol} برابر با {config['min_amount']} {symbol} است.")
        return
    if amount > config["max_amount"]:
        await message.answer(f"حداکثر خرید {symbol} برابر با {config['max_amount']} {symbol} است.")
        return

    await state.update_data(amount=str(amount), network=None, address=None)
    await state.set_state(BuyState.waiting_for_network)
    await message.answer(
        f"رمزارز: {config['name']} ({symbol})\n"
        f"مقدار خرید: {amount} {symbol}\n\n"
        "شبکه انتقال مقصد را انتخاب کن:",
        reply_markup=navigation_keyboard(),
    )
    await message.answer(
        "شبکه انتقال مقصد را انتخاب کن:",
        reply_markup=network_menu(symbol),
    )


# =====================================
# Handle network selection
# =====================================

@dp.callback_query(BuyState.waiting_for_network, F.data.startswith("network_"))
async def select_network(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    parts = (callback.data or "").split("_")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.message.answer("اطلاعات شبکه معتبر نیست. دوباره تلاش کن.")
        return

    symbol = parts[1]
    network_index = int(parts[2])
    data = await state.get_data()
    config = CRYPTO_CONFIG.get(symbol)

    if (
        not config
        or not config["enabled"]
        or data.get("crypto") != symbol
        or not 0 <= network_index < len(config["networks"])
    ):
        await callback.message.answer(
            "انتخاب شبکه معتبر نیست. لطفاً فرایند خرید را دوباره شروع کن."
        )
        return

    network = config["networks"][network_index]
    await state.update_data(network=network, address=None)
    await state.set_state(BuyState.waiting_for_address)
    await callback.message.answer(
        f"رمزارز: {symbol}\n"
        f"شبکه انتخاب‌شده: {network}\n\n"
        "آدرس کیف پول مقصد را روی همین شبکه وارد کن.",
        reply_markup=navigation_keyboard(),
    )


# =====================================
# Handle wallet address input and invoice creation
# =====================================

@dp.message(BuyState.waiting_for_address)
async def receive_address(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("لطفاً آدرس کیف پول را به‌صورت متن ارسال کن.")
        return

    address = message.text.strip()
    if not address:
        await message.answer("آدرس کیف پول نمی‌تواند خالی باشد.")
        return

    data = await state.get_data()
    symbol = data.get("crypto")
    network = data.get("network")
    amount_raw = data.get("amount")
    config = CRYPTO_CONFIG.get(symbol)

    if (
        not config
        or not config["enabled"]
        or network not in config["networks"]
        or not amount_raw
    ):
        await state.clear()
        await message.answer(
            "اطلاعات سفارش ناقص است. لطفاً از منوی خرید دوباره شروع کن.",
            reply_markup=main_menu(),
        )
        return

    try:
        amount = Decimal(amount_raw)
    except (InvalidOperation, TypeError):
        await state.clear()
        await message.answer("مقدار سفارش معتبر نیست. دوباره شروع کن.", reply_markup=main_menu())
        return

    if (
        not amount.is_finite()
        or amount < config["min_amount"]
        or amount > config["max_amount"]
    ):
        await state.clear()
        await message.answer(
            "مقدار سفارش خارج از محدوده مجاز است. دوباره شروع کن.",
            reply_markup=main_menu(),
        )
        return

    order = create_invoice(
        amount=amount,
        address=address,
        user_id=message.from_user.id,
        symbol=symbol,
        network=network,
    )
    await state.clear()
    await send_invoice(message, order)


# =====================================
# Handle invoice confirmation, cancellation, and renewal
# =====================================

@dp.callback_query(F.data.startswith("invoice_confirm:"))
async def confirm_invoice(callback: CallbackQuery):
    await callback.answer()
    order_id = callback.data.split(":", 1)[1]
    order = orders.get(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await callback.message.answer("سفارش پیدا نشد یا دسترسی به آن مجاز نیست.")
        return

    if order["status"] != "awaiting_confirmation":
        await callback.message.answer(f"این پیش‌فاکتور قابل تأیید نیست. وضعیت: {order['status']}")
        return

    if time.time() >= order["expires_at"]:
        order["status"] = "expired"
        stop_countdown(order_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=expired_invoice_keyboard(order_id))
        except TelegramBadRequest:
            pass
        await callback.message.answer("⏰ زمان پیش‌فاکتور تمام شده است. برای ادامه، قیمت را به‌روزرسانی کن.")
        return

    order["status"] = "confirmed_test"
    stop_countdown(order_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        "✅ پیش‌فاکتور در محیط آزمایشی تأیید شد.\n\n"
        f"شماره سفارش: {order_id}\n"
        f"مبلغ: {format_toman(order['total'])} تومان\n\n"
        "پرداخت واقعی یا ثبت سفارش در ایران اکسچنج انجام نشده است.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data.startswith("invoice_cancel:"))
async def cancel_invoice(callback: CallbackQuery):
    await callback.answer()
    order_id = callback.data.split(":", 1)[1]
    order = orders.get(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await callback.message.answer("سفارش پیدا نشد یا دسترسی به آن مجاز نیست.")
        return

    if order["status"] not in ("awaiting_confirmation", "expired"):
        await callback.message.answer(f"این سفارش دیگر قابل لغو نیست. وضعیت: {order['status']}")
        return

    order["status"] = "cancelled"
    stop_countdown(order_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(f"سفارش {order_id} لغو شد.", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("invoice_refresh:"))
async def refresh_invoice(callback: CallbackQuery):
    await callback.answer()
    old_id = callback.data.split(":", 1)[1]
    old_order = orders.get(old_id)

    if not old_order or old_order["user_id"] != callback.from_user.id:
        await callback.message.answer("سفارش پیدا نشد یا دسترسی به آن مجاز نیست.")
        return

    if old_order["status"] == "awaiting_confirmation":
        if time.time() < old_order["expires_at"]:
            await callback.message.answer("این پیش‌فاکتور هنوز معتبر است و می‌توانی آن را تأیید کنی.")
            return
        old_order["status"] = "expired"
        stop_countdown(old_id)

    if old_order["status"] != "expired":
        await callback.message.answer(
            "صدور مجدد برای این سفارش امکان‌پذیر نیست. "
            f"وضعیت فعلی: {old_order['status']}"
        )
        return

    old_order["status"] = "replaced"
    new_order = create_invoice(
        amount=old_order["amount"],
        address=old_order["address"],
        user_id=old_order["user_id"],
        symbol=old_order["crypto"],
        network=old_order["network"],
    )

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer("🔄 قیمت آزمایشی به‌روزرسانی شد؛ پیش‌فاکتور جدید صادر می‌شود.")
    await send_invoice(callback.message, new_order)


# =====================================
# Run the bot
# =====================================

async def main():
    if not TOKEN:
        raise RuntimeError(
            "توکن بات تنظیم نشده است. متغیر محیطی TELEGRAM_BOT_TOKEN را تنظیم کن."
        )

    bot = Bot(token=TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        for task in list(countdown_tasks.values()):
            task.cancel()
        if countdown_tasks:
            await asyncio.gather(*countdown_tasks.values(), return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
