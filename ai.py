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
Точность важнее скорости: не выдумывай данные. Если чего-то не знаешь или не уверен — проверь через инструменты (get_tasks, get_apartments, get_rent_status, get_finance, get_apartment_report и т.п.) или переспроси сэра, но не угадывай. Если вопрос про конкретные цифры/списки/статусы — сначала возьми реальные данные инструментом, потом отвечай. Если просьба неоднозначна — задай один уточняющий вопрос вместо того, чтобы сделать наугад.
Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.
Когда создаёшь задачу с дедлайном — всегда спрашивай: "Напомнить вам за день до дедлайна, сэр?" Если говорит да — ставь напоминание автоматически на 08:00 за день до дедлайна.
Если при закрытии или изменении задачи/договорённости находится несколько подходящих — переспроси сэра, какую именно он имеет в виду, не выбирай сам.
При записи финансов уточняй тип (расход или доход), если это не очевидно из контекста.

Учёт квартир (касса по сдаче квартир в субаренду) — отдельная система от личных финансов (finance), не путай их. Валюта операций по умолчанию MDL (лей); поддерживаются MDL, EUR, USD, USDT — ставь нужную в currency, если сэр называет другую. Способ оплаты (payment_method): по умолчанию "наличные"; если платят на карту — указывай "карта (укр)" или "карта (молд)" (касса считает их раздельно). Чтобы исправить ошибочную операцию — сначала get_apartment_report (там у каждой операции номер #id), потом edit_apartment_operation или delete_apartment_operation по этому номеру. При записи операции по квартире уточняй направление (приход/расход) и категорию (Аренда/Коммуналка/Депозит/Прочее), если не очевидно из контекста. Если адрес квартиры не найден или найдено несколько подходящих — переспроси сэра, не выбирай сам, и предложи добавить квартиру через add_apartment, если её действительно нет в справочнике. Сверку кассы (reconcile_apartment_balance) делай только когда сэр явно называет фактическую сумму на руках.
Когда сэр сообщает, что квартирант заплатил аренду (например "Иван заплатил 700 за Лев Толстой" или "за Арборилор 2 оплатили"), сразу вызови record_apartment_operation (direction='приход', category='Аренда', counterpart — имя квартиранта, currency='EUR' если не сказано иначе) — это важно, иначе оплата не попадёт в кассу и потеряется. Если сэр перечисляет несколько оплат сразу — запиши каждую отдельным вызовом. На вопросы "кто не заплатил / кто должен / кто оплатил аренду" используй get_rent_status.

У квартиры есть два разных понятия дня оплаты — не путай их: rent_day — день, когда МЫ платим аренду собственнику и собираем показания счётчиков; постоянное число месяца (1-31), не меняется при смене квартиранта (задаётся один раз через add_apartment вместе с owner_rent — суммой аренды, которую отдаём собственнику). И tenant_pay_day — день, когда ТЕКУЩИЙ квартирант платит аренду НАМ (день сбора оплаты с квартиранта); меняется при смене квартиранта, задаётся/обновляется через add_apartment вместе с tenant_rent. Когда сэр спрашивает про график сбора аренды с квартирантов ("когда забираем аренду у квартирантов", "список по квартирантам") — используй tenant_pay_day, а не rent_day. И lease_start/lease_end/tenant_rent/tenant_pay_day/deposit — данные ТЕКУЩЕГО квартиранта (период проживания, сумма его аренды, день оплаты, депозит), которые обновляются при каждом заселении. Когда сэр сообщает, что заехал новый квартирант "с такого-то по такое-то число", сначала вызови get_apartments, чтобы найти точный адрес этой квартиры как он записан в справочнике (квартира уже должна существовать), и вызови add_apartment с этим же адресом и lease_start/lease_end (формат YYYY-MM-DD), а также tenant_rent/tenant_pay_day/deposit, если сэр их называет — остальные поля не указывай, они не изменятся. Если адрес не нашёлся в справочнике — переспроси сэра, не создавай новую квартиру по неточному адресу. Бот сам каждый день в 8:00 проверяет: за день до rent_day (по числу месяца) — напоминает собрать показания счётчиков и сделать просчёт перед встречей; а в последние 10 дней перед lease_end — напоминает спросить квартиранта про продление или выезд (один раз за контракт). Дополнительно есть регулярные ежемесячные напоминания по SOP (sop_reminders) — фиксированные задачи по числам месяца (фактуры, газ, интернет и т.д.), которые бот тоже сам присылает в 8:00. По просьбе сэра показывай список (get_sop_reminders), добавляй (add_sop_reminder) или убирай (remove_sop_reminder) такие напоминания.

Расчёт коммуналки по счётчикам (calculate_utilities) — сэр называет новые показания (свет/газ/вода, иногда отопление/горячая вода — не у всех квартир), бот сам помнит прошлые показания, считает разницу × тариф и выводит разбивку с итогом. Тарифы (utility_tariffs) единые для всех квартир — если сэр говорит "тариф на газ теперь X" — вызови set_utility_tariff; текущие тарифы — get_utility_tariffs. Если для квартиры/услуги ещё нет сохранённого показания — текущее становится базовым, стоимость в этот раз 0. Фиксированная часть коммуналки (интернет и т.п., apartments.utilities_fixed) добавляется к итогу автоматически — задаётся/обновляется через add_apartment. Разовые статьи "по платёжке" (обслуживание дома, отопление в старых домах, уборка при выселении 500-1000 и т.п.) передавай через extra_items каждый раз отдельно, они не сохраняются. После расчёта, если сэр просит записать итог в кассу — отдельно вызови record_apartment_operation (приход, категория "Коммуналка").

Стиль: простой текст, язык — тот на котором пишет Юсеф. Для выделения важного (итоговые суммы, заголовки разделов в списках/отчётах) можно использовать *жирный* (одна звёздочка с каждой стороны) — Telegram отрендерит это жирным шрифтом. Не используй markdown-таблицы, заголовки с #, ---, двойные звёздочки ** , обратные кавычки и квадратные скобки. Обычные ответы — максимум 3-4 предложения; для списков/отчётов длина может быть больше."""


def build_system(context_str=""):
    """Системный промпт двумя блоками: статичная инструкция (кэшируется) + изменчивый хвост (дата/предпочтения/контекст)."""
    prefs = db_get_preferences()
    prefs_block = f"\n\nЗапомненные предпочтения сэра:\n{prefs}" if prefs else ""
    current_datetime = now_msk().strftime("%d.%m.%Y %H:%M")
    volatile = f"Дата: {current_datetime}{prefs_block}{context_str}"
    return [
        {"type": "text", "text": build_system_static(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile},
    ]


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
            "name": "add_apartment",
            "description": "Добавить квартиру в справочник учёта квартир, или обновить данные существующей (по адресу)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "address": {"type": "string"},
                    "owner_name": {"type": "string"},
                    "tenant_rent": {"type": "number", "description": "Сумма аренды, которую платит текущий квартирант (обычно задаётся при заселении)"},
                    "tenant_pay_day": {"type": "integer", "description": "Число месяца (1-31), когда ТЕКУЩИЙ квартирант платит аренду нам (день сбора оплаты с квартиранта). Меняется при смене квартиранта. Не путать с rent_day!"},
                    "owner_rent": {"type": "number", "description": "Сумма аренды, которую отдаём собственнику — постоянная для квартиры"},
                    "rent_day": {"type": "integer", "description": "Число месяца (1-31), когда МЫ платим аренду собственнику и собираем показания счётчиков. Постоянное для квартиры, не меняется при смене квартиранта. Не путать с tenant_pay_day!"},
                    "deposit": {"type": "number", "description": "Депозит текущего квартиранта (обычно задаётся при заселении)"},
                    "notes": {"type": "string"},
                    "lease_start": {"type": "string", "description": "Дата заезда текущего квартиранта, YYYY-MM-DD"},
                    "lease_end": {"type": "string", "description": "Дата выезда текущего квартиранта, YYYY-MM-DD"},
                    "utilities_fixed": {"type": "number", "description": "Фиксированная часть коммуналки в месяц (интернет и т.п.), автоматически прибавляется при расчёте через calculate_utilities"},
                    "floor": {"type": "string", "description": "Этаж"},
                    "unit_number": {"type": "string", "description": "Номер квартиры"},
                    "wifi_login": {"type": "string"},
                    "wifi_password": {"type": "string"},
                    "owner_contacts": {"type": "string", "description": "Телефон/телеграм собственника"}
                },
                "required": ["address"]
            }
        },
        {
            "name": "get_apartments",
            "description": "Получить справочник квартир (адрес, собственник, аренда, маржа, депозит)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "record_apartment_operation",
            "description": "Записать операцию в кассу по квартирам (приход или расход денег)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string", "description": "Адрес или часть адреса квартиры. Можно не указывать для общих операций"},
                    "direction": {"type": "string", "enum": ["приход", "расход"]},
                    "category": {"type": "string", "description": "Аренда, Коммуналка, Депозит, Прочее и т.п."},
                    "amount": {"type": "number"},
                    "currency": {"type": "string", "enum": ["MDL", "EUR", "USD", "USDT"], "description": "по умолчанию MDL"},
                    "payment_method": {"type": "string", "enum": ["наличные", "карта (укр)", "карта (молд)"], "description": "Как заплатили: наличные или на карту (украинскую/молдавскую). По умолчанию наличные"},
                    "counterpart": {"type": "string", "description": "Квартирант, Собственник, название провайдера и т.п."},
                    "date": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
                    "comment": {"type": "string"}
                },
                "required": ["direction", "category", "amount"]
            }
        },
        {
            "name": "edit_apartment_operation",
            "description": "Изменить существующую операцию кассы по её номеру (#id из get_apartment_report). Передавай только те поля, которые нужно поменять",
            "input_schema": {
                "type": "object",
                "properties": {
                    "op_id": {"type": "string", "description": "Номер операции (#id), узнать через get_apartment_report"},
                    "direction": {"type": "string", "enum": ["приход", "расход"]},
                    "category": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string", "enum": ["MDL", "EUR", "USD", "USDT"]},
                    "payment_method": {"type": "string", "enum": ["наличные", "карта (укр)", "карта (молд)"]},
                    "counterpart": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "comment": {"type": "string"}
                },
                "required": ["op_id"]
            }
        },
        {
            "name": "delete_apartment_operation",
            "description": "Удалить операцию кассы по её номеру (#id из get_apartment_report)",
            "input_schema": {
                "type": "object",
                "properties": {"op_id": {"type": "string", "description": "Номер операции (#id)"}},
                "required": ["op_id"]
            }
        },
        {
            "name": "get_apartment_balance",
            "description": "Получить расчётный баланс кассы по квартирам и результат последней сверки",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "reconcile_apartment_balance",
            "description": "Сверить расчётный баланс кассы по квартирам с фактической суммой на руках. Вызывай только когда сэр явно называет фактическую сумму",
            "input_schema": {
                "type": "object",
                "properties": {
                    "actual_balance": {"type": "number"},
                    "currency": {"type": "string", "description": "MDL или EUR, по умолчанию MDL"},
                    "comment": {"type": "string"}
                },
                "required": ["actual_balance"]
            }
        },
        {
            "name": "get_apartment_report",
            "description": "Получить операции по кассе квартир с фильтрами — для произвольных отчётов",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string"},
                    "category": {"type": "string"},
                    "direction": {"type": "string", "enum": ["приход", "расход"]},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"}
                }
            }
        },
        {
            "name": "get_rent_status",
            "description": "Показать, кто из квартирантов заплатил аренду за месяц, а кто нет. Используй на вопросы 'кто не заплатил', 'кто должен', 'кто оплатил аренду'. Оплата определяется по приходам категории «Аренда» в кассе квартир",
            "input_schema": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "Месяц в формате YYYY-MM, по умолчанию текущий"}
                }
            }
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
            "name": "get_utility_tariffs",
            "description": "Получить текущие тарифы на коммунальные услуги (свет, газ, вода и т.п.)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "set_utility_tariff",
            "description": "Задать или обновить тариф на коммунальную услугу (единый для всех квартир)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "utility_type": {"type": "string", "description": "свет, газ, вода, отопление, горячая вода и т.п."},
                    "tariff": {"type": "number", "description": "Цена за единицу (MDL за кВт·ч или м³)"}
                },
                "required": ["utility_type", "tariff"]
            }
        },
        {
            "name": "calculate_utilities",
            "description": "Рассчитать коммуналку для квартиры по новым показаниям счётчиков. Бот сам помнит прошлые показания и считает разницу × тариф, плюс фиксированную часть квартиры и доп. статьи",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string", "description": "Адрес или часть адреса квартиры"},
                    "readings": {
                        "type": "array",
                        "description": "Новые показания счётчиков",
                        "items": {
                            "type": "object",
                            "properties": {
                                "utility_type": {"type": "string", "description": "свет, газ, вода, отопление, горячая вода и т.п."},
                                "reading": {"type": "number"}
                            },
                            "required": ["utility_type", "reading"]
                        }
                    },
                    "extra_items": {
                        "type": "array",
                        "description": "Разовые дополнительные статьи: обслуживание дома по платёжке, отопление по платёжке, уборка при выселении и т.п.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "amount": {"type": "number"}
                            },
                            "required": ["description", "amount"]
                        }
                    }
                },
                "required": ["apartment", "readings"]
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
            "name": "get_sop_reminders",
            "description": "Получить список регулярных ежемесячных напоминаний по сопровождению квартир (SOP)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "add_sop_reminder",
            "description": "Добавить новое регулярное ежемесячное напоминание по сопровождению квартир (SOP)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "day_of_month": {"type": "integer", "description": "День месяца, 1-31"},
                    "text": {"type": "string"}
                },
                "required": ["day_of_month", "text"]
            }
        },
        {
            "name": "remove_sop_reminder",
            "description": "Убрать регулярное напоминание по SOP по части текста",
            "input_schema": {
                "type": "object",
                "properties": {"text_part": {"type": "string"}},
                "required": ["text_part"]
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
                elif block.name == "add_apartment":
                    db_add_apartment(inp["address"], **{f: inp.get(f) for f in APARTMENT_FIELDS})
                    result = f"Квартира сохранена: {inp['address']}"
                elif block.name == "get_apartments":
                    result = db_get_apartments()
                elif block.name == "record_apartment_operation":
                    status, info = db_record_apartment_operation(
                        inp.get("apartment"), inp["direction"], inp["category"], inp["amount"],
                        inp.get("currency", "MDL"), inp.get("counterpart"), inp.get("date"), inp.get("comment"),
                        inp.get("payment_method", "наличные")
                    )
                    if status == "recorded":
                        sign = "+" if inp["direction"] == "приход" else "-"
                        address_part = f" ({info})" if info else ""
                        method = inp.get("payment_method", "наличные")
                        method_part = f" [{method}]" if method and method != "наличные" else ""
                        result = f"Записано в кассу квартир{address_part}: {sign}{inp['amount']} {inp.get('currency', 'MDL')} [{inp['category']}]{method_part}"
                    elif status == "ambiguous":
                        result = "Нашлось несколько подходящих квартир, уточни у сэра какую он имеет в виду:\n" + "\n".join(f"- {a}" for a in info)
                    else:
                        result = f"Квартира '{info}' не найдена в справочнике. Уточни у сэра адрес или предложи добавить квартиру через add_apartment"
                elif block.name == "edit_apartment_operation":
                    status = db_update_apartment_operation(
                        inp["op_id"], direction=inp.get("direction"), category=inp.get("category"),
                        counterpart=inp.get("counterpart"), amount=inp.get("amount"), currency=inp.get("currency"),
                        comment=inp.get("comment"), payment_method=inp.get("payment_method"), op_date=inp.get("date")
                    )
                    result = {"updated": f"Операция #{inp['op_id']} обновлена",
                              "no_changes": "Не указано, что менять",
                              "not_found": "Операция с таким номером не найдена"}.get(status, "Не удалось обновить")
                elif block.name == "delete_apartment_operation":
                    status = db_delete_apartment_operation(inp["op_id"])
                    result = f"Операция #{inp['op_id']} удалена" if status == "deleted" else "Операция с таким номером не найдена"
                elif block.name == "get_apartment_balance":
                    result = db_get_apartment_balance()
                elif block.name == "reconcile_apartment_balance":
                    currency = inp.get("currency", "MDL")
                    expected, actual, diff = db_reconcile_apartment_balance(inp["actual_balance"], currency, inp.get("comment"))
                    if diff == 0:
                        result = f"Сверка ({currency}): расчётный баланс {expected} совпал с фактическим {actual}."
                    else:
                        result = f"Сверка ({currency}): расчётный баланс {expected}, по факту {actual}, расхождение {diff}. Записал корректирующую операцию."
                elif block.name == "get_apartment_report":
                    result = db_get_apartment_report(inp.get("apartment"), inp.get("category"), inp.get("direction"), inp.get("date_from"), inp.get("date_to"))
                elif block.name == "get_rent_status":
                    result = db_get_rent_status(inp.get("month"))
                elif block.name == "generate_chart":
                    chart_path = make_chart(inp["title"], inp["chart_type"], inp["labels"], inp["values"])
                    result = "График построен, будет отправлен сэру отдельным сообщением."
                elif block.name == "get_utility_tariffs":
                    result = db_get_utility_tariffs()
                elif block.name == "set_utility_tariff":
                    db_set_utility_tariff(inp["utility_type"], inp["tariff"])
                    result = f"Тариф обновлён: {inp['utility_type']} — {inp['tariff']} MDL/{UTILITY_UNITS.get(inp['utility_type'], 'ед.')}"
                elif block.name == "calculate_utilities":
                    status, info, lines, total = db_calculate_utilities(inp["apartment"], inp["readings"], inp.get("extra_items"))
                    if status == "ok":
                        result = f"Расчёт коммуналки для {info}:\n" + "\n".join(lines) + f"\n\nИТОГО: {total} MDL"
                    elif status == "ambiguous":
                        result = "Нашлось несколько подходящих квартир, уточни у сэра какую он имеет в виду:\n" + "\n".join(f"- {a}" for a in info)
                    else:
                        result = f"Квартира '{info}' не найдена в справочнике. Уточни у сэра адрес или предложи добавить квартиру через add_apartment"
                elif block.name == "save_preference":
                    db_save_preference(inp["key"], inp["value"])
                    result = f"Запомнено: {inp['key']}"
                elif block.name == "create_reminder":
                    db_create_reminder(inp["text"], inp["remind_at"])
                    result = f"Напоминание установлено на {inp['remind_at']}"
                elif block.name == "get_sop_reminders":
                    result = db_get_sop_reminders()
                elif block.name == "add_sop_reminder":
                    db_add_sop_reminder(inp["day_of_month"], inp["text"])
                    result = f"Напоминание добавлено на {inp['day_of_month']} число: {inp['text']}"
                elif block.name == "remove_sop_reminder":
                    status, items = db_remove_sop_reminder(inp["text_part"])
                    if status == "removed":
                        result = f"Напоминание убрано: {items[0]}"
                    elif status == "ambiguous":
                        result = "Нашлось несколько подходящих напоминаний, уточни у сэра какое убрать:\n" + "\n".join(f"- {t}" for t in items)
                    else:
                        result = "Напоминание не найдено"
            except Exception as e:
                logger.error(f"Tool {block.name} error: {e}")
                result = f"Ошибка при выполнении {block.name}: {e}. Если задача большая (много квартир/объектов), разбей её на несколько меньших шагов."
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]

    return (text or "Готово, сэр."), chart_path

