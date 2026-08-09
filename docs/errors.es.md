# Errors — Español

`53` error keys. Canonical texts live in the code (`register_service_errors`); localized texts in `translations/errors.es.json`.

| Código | Estado | Parámetros | Acción | Texto |
|---|---|---|---|---|
| `error.400.amount_invalid` | 400 | — | `fix_input` | El importe debe ser un número entero positivo |
| `error.400.bad_request` | 400 | — | `fix_input` | Solicitud incorrecta |
| `error.400.captcha_invalid` | 400 | — | `retry` | La verificación del captcha ha fallado. Inténtalo de nuevo. |
| `error.400.captcha_required` | 400 | — | `retry` | Se requiere el token del captcha. |
| `error.400.expected_list` | 400 | — | `fix_input` | Se esperaba una lista de elementos |
| `error.400.field.blank` | 400 | `field` | `fix_input` | {field} no puede estar vacío |
| `error.400.field.does_not_exist` | 400 | `field` | `fix_input` | {field} no existe |
| `error.400.field.invalid` | 400 | `field` | `fix_input` | {field} no es válido |
| `error.400.field.invalid_choice` | 400 | `field` | `fix_input` | {field} no es una opción válida |
| `error.400.field.max_length` | 400 | `field`, `max_length` | `fix_input` | {field} debe tener como máximo {max_length} caracteres |
| `error.400.field.max_value` | 400 | `field`, `max_value` | `fix_input` | {field} debe ser como máximo {max_value} |
| `error.400.field.min_length` | 400 | `field`, `min_length` | `fix_input` | {field} debe tener al menos {min_length} caracteres |
| `error.400.field.min_value` | 400 | `field`, `min_value` | `fix_input` | {field} debe ser como mínimo {min_value} |
| `error.400.field.null` | 400 | `field` | `fix_input` | {field} no puede ser nulo |
| `error.400.field.required` | 400 | `field` | `fix_input` | {field} es obligatorio |
| `error.400.field.unique` | 400 | `field` | `fix_input` | {field} debe ser único |
| `error.400.invalid_ad_id` | 400 | — | `fix_input` | ID de anuncio no válido |
| `error.400.invalid_package` | 400 | — | `fix_input` | Paquete de créditos no válido |
| `error.400.invalid_plan` | 400 | — | `fix_input` | Plan de suscripción no válido |
| `error.400.invalid_stripe_signature` | 400 | — | `contact_support` | Firma de webhook de Stripe no válida |
| `error.400.invalid_webhook_payload` | 400 | — | `contact_support` | Contenido de webhook de Stripe no válido |
| `error.400.redirect_url_not_configured` | 400 | — | `contact_support` | No se ha proporcionado la URL de redirección y no hay ninguna alternativa configurada. |
| `error.400.validation_error` | 400 | — | `fix_input` | Error de validación |
| `error.400.verification_failed` | 400 | — | `verify` | La verificación ha fallado |
| `error.400.verification_invalid_factor` | 400 | — | `verify` | Este factor de verificación no está disponible |
| `error.401.unauthorized` | 401 | — | `reauthenticate` | Se requiere autenticación |
| `error.402.insufficient_credits` | 402 | — | `fix_input` | Créditos insuficientes |
| `error.402.payment_required` | 402 | — | `retry` | Se requiere pago |
| `error.403.forbidden` | 403 | — | `retry` | No tienes permiso para realizar esta acción |
| `error.403.forbidden_billing` | 403 | — | `contact_support` | Prohibido: no puedes gestionar esta cuenta de facturación |
| `error.403.network_blocked` | 403 | — | `contact_support` | No se permiten solicitudes desde esta red. |
| `error.403.verification_enrollment_required` | 403 | — | `verify` | Es necesario registrar un factor de verificación. |
| `error.403.verification_required` | 403 | — | `verify` | Se requiere verificación adicional |
| `error.404.ad_not_found` | 404 | — | `retry` | Anuncio no encontrado |
| `error.404.not_found` | 404 | — | `retry` | Recurso solicitado no encontrado |
| `error.404.subscription_not_found` | 404 | — | `fix_input` | Suscripción no encontrada |
| `error.404.transaction_not_found` | 404 | — | `fix_input` | Transacción no encontrada |
| `error.404.verification_challenge_not_found` | 404 | — | `verify` | Desafío de verificación no encontrado o caducado |
| `error.404.wallet_not_found` | 404 | — | `fix_input` | Monedero no encontrado |
| `error.405.method_not_allowed` | 405 | — | `retry` | Método no permitido |
| `error.406.not_acceptable` | 406 | — | `retry` | No aceptable |
| `error.408.request_timeout` | 408 | — | `retry` | Tiempo de espera de la solicitud agotado |
| `error.409.conflict` | 409 | — | `fix_input` | El recurso ya existe |
| `error.409.duplicate_webhook_event` | 409 | — | `retry` | El evento de Stripe ya se ha procesado |
| `error.410.gone` | 410 | — | `retry` | El recurso se ha eliminado permanentemente |
| `error.413.payload_too_large` | 413 | — | `retry` | El cuerpo de la solicitud es demasiado grande |
| `error.415.unsupported_media_type` | 415 | — | `retry` | Tipo de contenido no compatible |
| `error.422.unprocessable_entity` | 422 | — | `wait_and_retry` | Entidad no procesable |
| `error.423.locked` | 423 | — | `wait_and_retry` | El recurso está bloqueado |
| `error.423.verification_locked` | 423 | — | `wait_and_retry` | Demasiados intentos fallidos — verificación bloqueada |
| `error.429.rate_limit` | 429 | `retry_after_minutes` | `wait_and_retry` | Demasiados intentos. Inténtalo de nuevo en {retry_after_minutes} minutos. |
| `error.429.too_many_requests` | 429 | — | `wait_and_retry` | Demasiadas solicitudes. Inténtalo de nuevo más tarde. |
| `error.500.internal` | 500 | — | `contact_support` | Algo salió mal |
