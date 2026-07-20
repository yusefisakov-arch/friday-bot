"""Слой ИИ: системный промпт, инструменты, обработка сообщения моделью, графики."""
import os
import uuid
import tempfile
import logging
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from anthropic import Anthropic

from core import *
from db import *

logger = logging.getLogger(__name__)
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def build_system_static():
    staff_lines = ", ".join(f"{name} ({role})" for name, role in STAFF.items())
    return f"""Ты FRIDAY — исполнительный ассистент Юсефа, предпринимателя (отели, апартаменты, общепит, крипто).
Сотрудники: {staff_lines}.
При делегировании — создавай задачу с пометкой исполнителя и задачу контроля для Юсефа.

Обращайся к нему "сэр". Говори как доверенный советник — прямо, коротко, без воды, но живо и по-человечески, а не сухо. Всегда подтверждай что зафиксировал.
Точность важнее скорости: не выдумывай данные. Если чего-то не знаешь или не уверен — проверь через инструменты (get_tasks, get_finance, get_goals и т.п.) или переспроси сэра, но не угадывай. Если вопрос про конкретные цифры/списки/статусы — сначала возьми реальные данные инструментом, потом отвечай. Если просьба неоднозначна — задай один уточняющий вопрос вместо того, чтобы сделать наугад.
Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.

Планирование и цели — твоя ключевая роль, относись к ней серьёзно. У сэра есть цели на день/неделю/месяц (set_goal, get_goals, update_goal, complete_goal, delete_goal). Помогай не просто записывать, а ДОВОДИТЬ до результата: когда сэр ставит цель — уточни срок (день/неделя/месяц) и при необходимости предложи разбить большую цель на конкретные шаги-задачи (create_task). Регулярно связывай задачи и цели: если задача двигает цель — отметь это. Когда сэр рассказывает, что сделал — обновляй прогресс цели (update_goal). Каждое утро бот показывает цели и помогает спланировать день; в начале недели и месяца — помогает спланировать неделю/месяц и подводит итоги прошлого периода. Будь проактивен: если на сегодня нет целей — предложи поставить 1-3 главные; если цель давно без прогресса — мягко напомни. Не выдумывай цели сам, но активно подталкивай и предлагай формулировки.
Ты ещё и наставник: вечером (и когда сэр просит «разбери день», «как я иду к целям», «наставничество») анализируй его прогресс по реальным данным (get_goals, get_tasks, get_finance и т.п.), честно говори, где он отстаёт или не успевает, и давай конкретные советы как поступить — что приоритезировать, что отложить, как разбить застрявшую цель. Поддерживай, но будь прямым; держи его в фокусе на целях.
У сэра трудности с дисциплиной — помогай ему держаться: когда он обещает что-то сделать ко времени ("позвоню в 15:00", "сделаю к вечеру") — предложи поставить контроль-пинг через create_reminder с текстом вида "Сделал: <что>?" на это время, чтобы потом спросить. Если задачу переносят/откладывают много раз — не давай ей тихо висеть: предложи разбить на маленький первый шаг, делегировать или убрать.
Когда создаёшь задачу с дедлайном — всегда спрашивай: "Напомнить вам за день до дедлайна, сэр?" Если говорит да — ставь напоминание автоматически на 08:00 за день до дедлайна.
Если при закрытии или изменении задачи/договорённости находится несколько подходящих — переспроси сэра, какую именно он имеет в виду, не выбирай сам.
При записи финансов уточняй тип (расход или доход), если это не очевидно из контекста.




Стиль: простой текст, язык — тот на котором пишет Юсеф. Для выделения важного (итоговые суммы, заголовки разделов в списках/отчётах) можно использовать *жирный* (одна звёздочка с каждой стороны) — Telegram отрендерит это жирным шрифтом. Не используй markdown-таблицы, заголовки с #, ---, двойные звёздочки ** , обратные кавычки и квадратные скобки. Обычные ответы — максимум 3-4 предложения; для списков/отчётов длина может быть больше.

ВАЖНО про подачу (у сэра загружен мозг, читать должно быть легко): любой длинный ответ начинай с одной строки-итога TL;DR (например "Итог: 3 дела, 1 срочное, 2 оплаты"). Дальше — короткие пункты, главное сверху, группируй по смыслу с *жирными* подзаголовками и эмодзи-маркерами. В списках задач/дел в конце добавляй строку "🎯 Сегодня главное закрыть: …" с 1-3 самыми важными и коротко почему (просрочено / высокий приоритет / двигает срок). Не лей воду, пиши структурно и сканируемо."""


HARD_MODE_INSTRUCTION = ("\n\nЖЁСТКИЙ РЕЖИМ ВКЛЮЧЁН: будь прямым и требовательным, без поблажек и реверансов. "
                         "Называй отговорки отговорками, дави на действие сейчас, напоминай про просроченное и переносы, "
                         "не давай соскользнуть. Уважительно, но жёстко.")


def hard_mode_on():
    return db_get_setting("hard_mode", "off") == "on"


def build_system(context_str=""):
    """Системный промпт двумя блоками: статичная инструкция (кэшируется) + изменчивый хвост (дата/предпочтения/контекст)."""
    prefs = db_get_preferences()
    prefs_block = f"\n\nЗапомненные предпочтения сэра:\n{prefs}" if prefs else ""
    current_datetime = now_msk().strftime("%d.%m.%Y %H:%M")
    hard = HARD_MODE_INSTRUCTION if hard_mode_on() else ""
    volatile = f"Дата: {current_datetime}{prefs_block}{context_str}{hard}"
    return [
        {"type": "text", "text": build_system_static(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile},
    ]


MENTOR_INSTRUCTIONS = """Сейчас ты в режиме НАСТАВНИКА. Юсеф хочет, чтобы ты не просто фиксировал, а помогал ему расти и доводить цели до результата.
Проанализируй данные за день и движение к целям (день/неделя/месяц). Дай честную, но поддерживающую оценку: что удалось, где он отстаёт или буксует. Если видно, что не успевает или не справляется — прямо скажи об этом и предложи 2-3 КОНКРЕТНЫХ шага: что сделать завтра, что важнее всего, что отложить или делегировать, как разбить застрявшую цель. Будь конкретным советником, а не флегматиком: не лей воду, опирайся на реальные цифры из данных. Отметь серию (стрик), если она есть — это мотивирует не прерывать. В конце ОБЯЗАТЕЛЬНО попроси назвать Top-3 главные дела на завтра (зафиксируешь их через set_goal horizon=day) и заверши этим вопросом. Пиши тепло, по-человечески, на «вы»/«сэр». Не выдумывай факты, которых нет в данных."""


def generate_mentor_briefing():
    """Вечерний разбор-наставничество: анализ дня и целей, генерируется моделью."""
    digest = db_get_day_activity()
    goals = db_get_goals()
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now_msk().weekday()]
    user_msg = (f"Сегодня {now_msk().strftime('%d.%m.%Y')}, {weekday}.\n\n"
                f"Данные за день:\n{digest}\n\nЦели:\n{goals}\n\n"
                "Сформируй вечерний разбор-наставничество по этим данным.")
    system = build_system_static() + "\n\n" + MENTOR_INSTRUCTIONS + (HARD_MODE_INSTRUCTION if hard_mode_on() else "")
    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "Не удалось собрать вечерний разбор, сэр."


def make_chart(title, chart_type, labels, values):
    fig, ax = plt.subplots(figsize=(6, 4))
    if chart_type == "pie":
        ax.pie(values, labels=labels, autopct="%1.0f%%")
    elif chart_type == "line":
        ax.plot(labels, values, marker="o")
        ax.tick_params(axis="x", rotation=45)
    else:
        ax.bar(labels, values)
        ax.tick_params(axis="x", rotation=45)
    ax.set_title(title)
    fig.tight_layout()
    path = os.path.join(tempfile.gettempdir(), f"chart_{uuid.uuid4().hex}.png")
    fig.savefig(path)
    plt.close(fig)
    return path


_HEAT_STATUS = {"green": "оплачено", "red": "забрать!", "yellow": "скоро", "grey": "рано", "none": "без аренды"}


def process_message(messages, system):
    tools = [
        {
            "name": "create_task",
            "description": "Создать задачу",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"},
                    "assignee": {"type": "string", "description": "Кому поручено, если делегируется, например: " + ", ".join(STAFF.keys())}
                },
                "required": ["name"]
            }
        },
        {
            "name": "get_tasks",
            "description": "Получить список задач",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_task",
            "description": "Закрыть задачу как выполненную. В name_part можно передать номер задачи (#123 или 123) — так надёжнее, особенно если есть одинаковые задачи",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string", "description": "Номер задачи (#123) или часть названия"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "delete_task",
            "description": "Полностью удалить задачу по номеру (для дублей или мусора). Узнать номера можно через get_tasks — они показаны как #123",
            "input_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Номер задачи, например 123 или #123"}},
                "required": ["task_id"]
            }
        },
        {
            "name": "update_task",
            "description": "Изменить дедлайн, приоритет и/или исполнителя существующей открытой задачи",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_part": {"type": "string", "description": "Номер задачи (#123) или часть названия"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"},
                    "assignee": {"type": "string", "description": "Кому поручено, например: " + ", ".join(STAFF.keys())}
                },
                "required": ["name_part"]
            }
        },
        {
            "name": "find_task",
            "description": "Найти задачу по части названия, включая уже закрытые. Используй чтобы проверить статус или историю задачи",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "create_decision",
            "description": "Записать договорённость",
            "input_schema": {
                "type": "object",
                "properties": {
                    "with_whom": {"type": "string"},
                    "what_decided": {"type": "string"},
                    "deadline": {"type": "string"},
                    "next_step": {"type": "string"}
                },
                "required": ["with_whom", "what_decided"]
            }
        },
        {
            "name": "get_decisions",
            "description": "Получить список открытых договорённостей",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_decision",
            "description": "Отметить договорённость выполненной",
            "input_schema": {
                "type": "object",
                "properties": {"text_part": {"type": "string", "description": "Часть текста договорённости или имени, с кем договорились"}},
                "required": ["text_part"]
            }
        },
        {
            "name": "create_finance",
            "description": "Записать расход или доход",
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Сумма, положительное число"},
                    "category": {"type": "string"},
                    "type": {"type": "string", "enum": ["расход", "доход"], "description": "расход или доход"},
                    "currency": {"type": "string", "enum": ["MDL", "EUR", "USD", "USDT"], "description": "Валюта, по умолчанию MDL"},
                    "comment": {"type": "string"}
                },
                "required": ["amount", "category", "type"]
            }
        },
        {
            "name": "get_finance",
            "description": "Получить финансовые записи и итоги за месяц",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "generate_chart",
            "description": "Построить картинку-график по готовым данным (например, из get_apartment_report) для наглядного отчёта",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "chart_type": {"type": "string", "enum": ["bar", "pie", "line"]},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "array", "items": {"type": "number"}}
                },
                "required": ["title", "chart_type", "labels", "values"]
            }
        },
        {
            "name": "save_preference",
            "description": "Запомнить предпочтение или важную информацию",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"}
                },
                "required": ["key", "value"]
            }
        },
        {
            "name": "create_reminder",
            "description": "Создать напоминание на конкретное время. Используй когда говорят 'напомни через X' или 'напомни в X'",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст напоминания"},
                    "remind_at": {"type": "string", "description": "Время в формате YYYY-MM-DD HH:MM"}
                },
                "required": ["text", "remind_at"]
            }
        },
        {
            "name": "set_goal",
            "description": "Поставить цель на день, неделю или месяц. Используй, когда сэр формулирует цель/намерение ('хочу', 'цель на неделю', 'в этом месяце нужно')",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Формулировка цели"},
                    "horizon": {"type": "string", "enum": ["day", "week", "month"], "description": "Горизонт: день/неделя/месяц"}
                },
                "required": ["text", "horizon"]
            }
        },
        {
            "name": "get_goals",
            "description": "Показать активные цели (на день/неделю/месяц) с прогрессом",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "update_goal",
            "description": "Обновить цель по номеру (#id): записать прогресс или поменять формулировку",
            "input_schema": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Номер цели (#id) из get_goals"},
                    "progress": {"type": "string", "description": "Заметка о прогрессе"},
                    "text": {"type": "string", "description": "Новая формулировка цели"}
                },
                "required": ["goal_id"]
            }
        },
        {
            "name": "complete_goal",
            "description": "Отметить цель достигнутой по номеру (#id)",
            "input_schema": {
                "type": "object",
                "properties": {"goal_id": {"type": "string"}},
                "required": ["goal_id"]
            }
        },
        {
            "name": "delete_goal",
            "description": "Удалить цель по номеру (#id) — если она больше не актуальна",
            "input_schema": {
                "type": "object",
                "properties": {"goal_id": {"type": "string"}},
                "required": ["goal_id"]
            }
        }
    ]

    # Кэшируем массив инструментов (самая большая статичная часть запроса) —
    # повторные запросы за ним стоят в ~10 раз дешевле.
    tools[-1]["cache_control"] = {"type": "ephemeral"}

    messages = list(messages)
    text = ""
    chart_path = None
    for _ in range(5):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=tools
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            break

        tool_results = []
        for block in tool_uses:
            inp = block.input
            result = ""
            try:
                if block.name == "create_task":
                    db_create_task(inp["name"], inp.get("deadline"), inp.get("priority"), inp.get("assignee"))
                    result = f"Задача создана: {inp['name']}"
                elif block.name == "get_tasks":
                    result = db_get_tasks()
                elif block.name == "close_task":
                    status, items = db_close_task(inp["name_part"])
                    if status == "closed":
                        result = f"Задача закрыта: {items[0]}"
                    elif status == "ambiguous":
                        result = ("Нашлось несколько подходящих задач. Покажи сэру с номерами и закрой по нужному номеру (#id):\n"
                                  + "\n".join(f"- {n}" for n in items))
                    else:
                        result = "Задача не найдена"
                elif block.name == "delete_task":
                    status, name = db_delete_task(inp["task_id"])
                    result = f"Задача удалена: {name}" if status == "deleted" else "Задача с таким номером не найдена"
                elif block.name == "update_task":
                    status, items = db_update_task(inp["name_part"], inp.get("deadline"), inp.get("priority"), inp.get("assignee"))
                    if status == "updated":
                        result = f"Задача обновлена: {items[0]}"
                    elif status == "ambiguous":
                        result = "Нашлось несколько подходящих задач, уточни у сэра какую менять:\n" + "\n".join(f"- {n}" for n in items)
                    else:
                        result = "Задача не найдена"
                elif block.name == "find_task":
                    result = db_find_task(inp["name_part"])
                elif block.name == "create_decision":
                    db_create_decision(inp["with_whom"], inp["what_decided"], inp.get("deadline"), inp.get("next_step"))
                    result = f"Записано: {inp['with_whom']} — {inp['what_decided']}"
                elif block.name == "get_decisions":
                    result = db_get_decisions()
                elif block.name == "close_decision":
                    status, items = db_close_decision(inp["text_part"])
                    if status == "closed":
                        result = f"Договорённость закрыта: {items[0]}"
                    elif status == "ambiguous":
                        result = "Нашлось несколько подходящих договорённостей, уточни у сэра какую закрыть:\n" + "\n".join(f"- {n}" for n in items)
                    else:
                        result = "Договорённость не найдена"
                elif block.name == "create_finance":
                    currency = inp.get("currency", "MDL")
                    db_create_finance(inp["amount"], inp["category"], inp.get("type", "расход"), inp.get("comment"), currency)
                    sign = "-" if inp.get("type", "расход") == "расход" else "+"
                    result = f"Записано: {inp['category']} {sign}{inp['amount']} {currency}"
                elif block.name == "get_finance":
                    result = db_get_finance()
                elif block.name == "generate_chart":
                    chart_path = make_chart(inp["title"], inp["chart_type"], inp["labels"], inp["values"])
                    result = "График построен, будет отправлен сэру отдельным сообщением."
                elif block.name == "save_preference":
                    db_save_preference(inp["key"], inp["value"])
                    result = f"Запомнено: {inp['key']}"
                elif block.name == "create_reminder":
                    db_create_reminder(inp["text"], inp["remind_at"])
                    result = f"Напоминание установлено на {inp['remind_at']}"
                elif block.name == "set_goal":
                    gid = db_create_goal(inp["text"], inp.get("horizon", "day"))
                    result = f"Цель поставлена (#{gid}): {inp['text']}"
                elif block.name == "get_goals":
                    result = db_get_goals()
                elif block.name == "update_goal":
                    status = db_update_goal(inp["goal_id"], progress=inp.get("progress"), text=inp.get("text"))
                    result = {"updated": f"Цель #{inp['goal_id']} обновлена",
                              "no_changes": "Не указано, что обновить",
                              "not_found": "Цель с таким номером не найдена"}.get(status, "Не удалось обновить")
                elif block.name == "complete_goal":
                    status = db_update_goal(inp["goal_id"], status="done")
                    result = f"Цель #{inp['goal_id']} достигнута 🎉" if status == "updated" else "Цель с таким номером не найдена"
                elif block.name == "delete_goal":
                    status = db_delete_goal(inp["goal_id"])
                    result = f"Цель #{inp['goal_id']} удалена" if status == "deleted" else "Цель с таким номером не найдена"
            except Exception as e:
                logger.error(f"Tool {block.name} error: {e}")
                result = f"Ошибка при выполнении {block.name}: {e}. Если задача большая (много квартир/объектов), разбей её на несколько меньших шагов."
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]

    return (text or "Готово, сэр."), chart_path

