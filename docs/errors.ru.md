# Errors — Русский

`53` error keys. Canonical texts live in the code (`register_service_errors`); localized texts in `translations/errors.ru.json`.

| Код | Статус | Параметры | Действие | Текст |
|---|---|---|---|---|
| `error.400.amount_invalid` | 400 | — | `fix_input` | Сумма должна быть положительным целым числом |
| `error.400.bad_request` | 400 | — | `fix_input` | Некорректный запрос |
| `error.400.captcha_invalid` | 400 | — | `retry` | Проверка капчи не пройдена. Пожалуйста, попробуйте ещё раз. |
| `error.400.captcha_required` | 400 | — | `retry` | Требуется токен капчи. |
| `error.400.expected_list` | 400 | — | `fix_input` | Ожидался список элементов |
| `error.400.field.blank` | 400 | `field` | `fix_input` | Поле «{field}» не может быть пустым |
| `error.400.field.does_not_exist` | 400 | `field` | `fix_input` | «{field}» не существует |
| `error.400.field.invalid` | 400 | `field` | `fix_input` | Поле «{field}» содержит недопустимое значение |
| `error.400.field.invalid_choice` | 400 | `field` | `fix_input` | Недопустимый вариант для поля «{field}» |
| `error.400.field.max_length` | 400 | `field`, `max_length` | `fix_input` | Поле «{field}» должно содержать не более {max_length} символов |
| `error.400.field.max_value` | 400 | `field`, `max_value` | `fix_input` | Значение поля «{field}» должно быть не больше {max_value} |
| `error.400.field.min_length` | 400 | `field`, `min_length` | `fix_input` | Поле «{field}» должно содержать не менее {min_length} символов |
| `error.400.field.min_value` | 400 | `field`, `min_value` | `fix_input` | Значение поля «{field}» должно быть не меньше {min_value} |
| `error.400.field.null` | 400 | `field` | `fix_input` | Поле «{field}» не может быть null |
| `error.400.field.required` | 400 | `field` | `fix_input` | Поле «{field}» обязательно |
| `error.400.field.unique` | 400 | `field` | `fix_input` | Значение поля «{field}» должно быть уникальным |
| `error.400.invalid_ad_id` | 400 | — | `fix_input` | Недопустимый идентификатор объявления |
| `error.400.invalid_package` | 400 | — | `fix_input` | Недопустимый пакет кредитов |
| `error.400.invalid_plan` | 400 | — | `fix_input` | Недопустимый план подписки |
| `error.400.invalid_stripe_signature` | 400 | — | `contact_support` | Недействительная подпись вебхука Stripe |
| `error.400.invalid_webhook_payload` | 400 | — | `contact_support` | Недопустимые данные вебхука Stripe |
| `error.400.redirect_url_not_configured` | 400 | — | `contact_support` | redirect_url не передан в запросе, а запасной URL не настроен. |
| `error.400.validation_error` | 400 | — | `fix_input` | Ошибка валидации |
| `error.400.verification_failed` | 400 | — | `verify` | Проверка не пройдена |
| `error.400.verification_invalid_factor` | 400 | — | `verify` | Этот способ подтверждения недоступен |
| `error.401.unauthorized` | 401 | — | `reauthenticate` | Требуется аутентификация |
| `error.402.insufficient_credits` | 402 | — | `fix_input` | Недостаточно кредитов |
| `error.402.payment_required` | 402 | — | `retry` | Требуется оплата |
| `error.403.forbidden` | 403 | — | `retry` | У вас нет прав для выполнения этого действия |
| `error.403.forbidden_billing` | 403 | — | `contact_support` | Доступ запрещён: нельзя управлять этим платёжным аккаунтом |
| `error.403.network_blocked` | 403 | — | `contact_support` | Запросы из этой сети не разрешены. |
| `error.403.verification_enrollment_required` | 403 | — | `verify` | Требуется регистрация фактора подтверждения. |
| `error.403.verification_required` | 403 | — | `verify` | Требуется дополнительная проверка |
| `error.404.ad_not_found` | 404 | — | `retry` | Объявление не найдено |
| `error.404.not_found` | 404 | — | `retry` | Запрошенный ресурс не найден |
| `error.404.subscription_not_found` | 404 | — | `fix_input` | Подписка не найдена |
| `error.404.transaction_not_found` | 404 | — | `fix_input` | Транзакция не найдена |
| `error.404.verification_challenge_not_found` | 404 | — | `verify` | Запрос на подтверждение не найден или истёк |
| `error.404.wallet_not_found` | 404 | — | `fix_input` | Кошелёк не найден |
| `error.405.method_not_allowed` | 405 | — | `retry` | Метод не разрешён |
| `error.406.not_acceptable` | 406 | — | `retry` | Недопустимый формат ответа |
| `error.408.request_timeout` | 408 | — | `retry` | Время ожидания запроса истекло |
| `error.409.conflict` | 409 | — | `fix_input` | Ресурс уже существует |
| `error.409.duplicate_webhook_event` | 409 | — | `retry` | Событие Stripe уже обработано |
| `error.410.gone` | 410 | — | `retry` | Ресурс был безвозвратно удалён |
| `error.413.payload_too_large` | 413 | — | `retry` | Тело запроса слишком большое |
| `error.415.unsupported_media_type` | 415 | — | `retry` | Неподдерживаемый тип данных |
| `error.422.unprocessable_entity` | 422 | — | `wait_and_retry` | Невозможно обработать данные запроса |
| `error.423.locked` | 423 | — | `wait_and_retry` | Ресурс заблокирован |
| `error.423.verification_locked` | 423 | — | `wait_and_retry` | Слишком много неудачных попыток — подтверждение заблокировано |
| `error.429.rate_limit` | 429 | `retry_after_minutes` | `wait_and_retry` | Слишком много попыток. Повторите попытку через {retry_after_minutes} мин. |
| `error.429.too_many_requests` | 429 | — | `wait_and_retry` | Слишком много запросов. Пожалуйста, повторите попытку позже. |
| `error.500.internal` | 500 | — | `contact_support` | Что-то пошло не так |
