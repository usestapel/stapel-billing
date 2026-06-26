def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "stapel_core.django.users",
                "rest_framework",
                "stapel_billing",
            ],
            AUTH_USER_MODEL="users.User",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            ROOT_URLCONF="stapel_billing.urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # In-memory bus — no Kafka/Redis broker needed
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            # Skip migrations — create tables directly from models
            MIGRATION_MODULES={
                "users": None,
                "billing": None,
            },
        )
