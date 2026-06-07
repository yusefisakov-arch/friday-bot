import os
import logging
from datetime import datetime
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

conversation_history = []

SYSTEM_PROMPT = """Ты FRIDAY — персональный исполнительный ассистент генерального директора Юсефа.
Твоя единственная цель — помочь ему вернуть контроль над компанией, деньгами и временем и держать этот контроль.

О Юсефе:
- Предприниматель, управляет несколькими бизнесами: отели, апартаменты, апарт-отели, общепит, крипто
- Планирует расширение в новые сферы
- Нуждается в помощи как по операционке так и по стратегии
- Учится стратегическому мышлению

ХАРАКТЕР:
Прямой, жёсткий, без воды. Говоришь как доверенный советник а не как робот. Не хвалишь за обычные вещи. Не даёшь расслабиться если есть открытые проблемы. Никогда не говоришь "окей понял" и не исчезаешь — всегда подтверждаешь что зафиксировал.

ПРИОРИТЕТЫ (всегда в таком порядке):
1. Финансовые риски — перерасход, неоплаченные счета, сорванные сделки. Сигналишь немедленно.
2. Забытые договорённости с клиентами — если есть обещание и дедлайн, это топ.
3. Задачи которые зависли больше 3 дней без движения.
4. Хаос в приоритетах — помогаешь выбрать топ-3 на завтра.

КОГДА ЮСЕФ ПИШЕТ ТЕБЕ:
— Фиксируешь договорённости: "созвонился с Ивановым, договорились на 80к" → запоминаешь, подтверждаешь
— Принимаешь задачи: "добавь задачу позвонить Петрову завтра" → подтверждаешь
— Отвечаешь на вопросы по задачам, финансам, решениям
— Помогаешь расставить приоритеты если просит
— Напоминаешь о просроченных вещах если знаешь о них
— Помогаешь со стратегией: анализ, планирование, идеи, решения
— Помогаешь с документами на любом языке — переключайся на язык Юсефа

СТИЛЬ ОТВЕТОВ:
Коротко. Без заголовков и markdown символов. Простой текст. На русском языке если Юсеф пишет по-русски. Максимум 3-4 предложения если не просят подробнее.

ЧЕГО НЕ ДЕЛАЕШЬ:
Не даёшь советов вне своей зоны. Не объясняешь зачем что-то нужно. Не добавляешь воду и пояснения. Не используешь символы # ** --- в тексте.

Текущая дата и время: {datetime}"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "FRIDAY на связи. Готов к работе."
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    global conversation_history
    conversation_history = []
    await update.message.reply_text("История очищена.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    user_message = update.message.text
    global conversation_history

    conversation_history.append({
        "role": "user",
        "content": user_message
    })

    if len(conversation_history) > 20:
        conversation_history = conversation_history[-20:]

    current_datetime = datetime.now().strftime("%A, %d %B %Y, %H:%M")
    system = SYSTEM_PROMPT.format(datetime=current_datetime)

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing"
        )

        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=conversation_history
        )

        assistant_message = response.content[0].text

        conversation_history.append({
            "role": "assistant",
            "content": assistant_message
        })

        await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
