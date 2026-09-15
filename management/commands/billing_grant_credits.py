"""Put credits on an account from a terminal.

The affordance that was missing. Until this command there was exactly one
way to grant credits without taking a payment — an admin action behind a
browser session — so anybody without that session (an operator on the box,
a deploy script, a support engineer over ssh) had no way at all, and a
deployment's own staff could not get the credits they needed to test the
product they were shipping.

    manage.py billing_grant_credits --account <id|e-mail> --credits 100 \
        --reason "staff testing before the 2026-09-16 release" \
        --actor ops@example.com

On a containerised deployment that is::

    docker exec <billing container> python manage.py billing_grant_credits \
        --account someone@example.com --credits 100 \
        --reason "..." --actor "..." --idempotency-key grant-2026-09-16-01

WHAT IT REFUSES, and why each refusal is worth a non-zero exit:

* an account nothing matches, or an e-mail more than one account matches —
  a grant that lands on nobody, or on the wrong one of two rows, is worse
  than no grant, because the operator walks away believing it worked;
* zero or a negative amount — "grant nothing" is never what anybody meant;
* an empty reason or actor — a credit adjustment nobody can explain and
  nobody signed is the row an audit stops on.

``--idempotency-key`` is the safety on a repeat: the same key on the same
account grants once, however many times the command runs, and the second
run says so rather than pretending it granted again. Give one whenever the
command is in a script, a retry loop, or a runbook someone may follow
twice. Without a key, two runs are two grants — which is also correct,
because "give them another 100" is a real instruction and the command must
not make it impossible.

``--dry-run`` resolves the account and reports the balance it would change,
writing nothing.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ... import services


class Command(BaseCommand):
    help = (
        "Grant credits to one account by id or e-mail, recording the reason "
        "and the actor on an `adjustment` ledger row."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            required=True,
            metavar="ID_OR_EMAIL",
            help="The account to credit: its primary key, or its e-mail address.",
        )
        parser.add_argument(
            "--credits",
            required=True,
            type=int,
            metavar="N",
            help="How many credits to add. Must be positive.",
        )
        parser.add_argument(
            "--reason",
            required=True,
            metavar="TEXT",
            help=(
                "Why. Written onto the ledger row and is the only "
                "explanation an audit will find."
            ),
        )
        parser.add_argument(
            "--actor",
            required=True,
            metavar="WHO",
            help=(
                "Who decided — a person, a team or a script. Recorded on "
                "the ledger row beside the reason."
            ),
        )
        parser.add_argument(
            "--idempotency-key",
            dest="idempotency_key",
            default=None,
            metavar="KEY",
            help=(
                "Make the grant repeat-safe: the same key on the same "
                "account grants exactly once. Strongly recommended from a "
                "script or a runbook."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve the account and report; write nothing.",
        )

    def handle(self, *args, **options):
        account_ref = options["account"]
        credits = options["credits"]
        reason = options["reason"]
        actor = options["actor"]
        key = options["idempotency_key"]
        dry_run = bool(options["dry_run"])

        # Validate the AMOUNT before touching the database: an operator who
        # typed -50 should be told so whether or not the account exists.
        if credits <= 0:
            raise CommandError(
                f"--credits must be positive, got {credits}. "
                "Nothing was granted."
            )
        if not (reason or "").strip():
            raise CommandError("--reason must not be empty. Nothing was granted.")
        if not (actor or "").strip():
            raise CommandError("--actor must not be empty. Nothing was granted.")

        try:
            user = services.resolve_account(account_ref)
        except services.AmbiguousAccountError as exc:
            raise CommandError(f"{exc} Nothing was granted.") from None
        except services.AccountNotFoundError as exc:
            raise CommandError(f"{exc}. Nothing was granted.") from None

        # Short ids in the output, never the address: this command's
        # transcript ends up in deploy logs and chat.
        short = str(getattr(user, "pk", "?"))[:8]
        wallet = services.get_or_create_wallet(user)
        before = wallet.balance

        if dry_run:
            self.stdout.write(
                f"account {short}  wallet {str(wallet.id)[:8]}  "
                f"balance {before} -> {before + credits}  "
                f"({credits} credit(s); dry run — nothing was written)"
            )
            return

        # Taken before the call so that "is this row the one I just wrote"
        # is answered by the row's own age rather than by comparing
        # balances, which two different amounts under one key can make
        # agree by accident.
        started = timezone.now()
        with transaction.atomic():
            try:
                txn = services.grant_credits(
                    user=user,
                    credits=credits,
                    reason=reason,
                    actor=actor,
                    idempotency_key=key,
                )
            except ValueError as exc:
                raise CommandError(f"{exc}. Nothing was granted.") from None

        wallet.refresh_from_db()
        after = wallet.balance
        replayed = txn.created_at < started

        self.stdout.write(f"account     {short}")
        self.stdout.write(f"wallet      {str(wallet.id)[:8]}")
        self.stdout.write(f"transaction {txn.id}")
        self.stdout.write(f"type        {txn.type}")
        self.stdout.write(f"delta       {txn.credits_delta:+d}")
        self.stdout.write(f"balance     {before} -> {after}")
        if replayed:
            # The wallet did not move and a key was given: services.credit
            # short-circuited on it. Say so — "granted" when nothing moved
            # is the lie that makes an idempotent command untrustworthy.
            self.stdout.write(
                self.style.WARNING(
                    f"already granted under idempotency key {key!r} — "
                    "nothing moved this time"
                )
            )
            return
        self.stdout.write(
            self.style.SUCCESS(f"granted {credits} credit(s) to account {short}")
        )
