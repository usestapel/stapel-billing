"""Give a customer subscription time, on the house.

The sanctioned shape of "put them back on Pro for a month". It writes a
**comp period** — a local row the entitlement surface honours once the
provider's own period stops entitling — and touches Stripe not at all.

It deliberately does NOT edit ``Subscription.current_period_end``: that
column mirrors the provider, so the next subscription webhook re-reads it
and the gift silently disappears days later.

    manage.py billing_extend_subscription --user <id> --days 60 --reason "outage"
    manage.py billing_extend_subscription --user <id> --days 60 --reason "outage" --apply

Dry run is the default: the output is the same either way, and the run
that writes should not be the run that tells you what it will write.
"""

import getpass

from django.core.management.base import BaseCommand, CommandError

from ... import services
from ...models import Subscription


class Command(BaseCommand):
    help = (
        "Extend one subscription with comp time (a local entitlement "
        "window). Dry run unless --apply."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--user",
            dest="user",
            metavar="USER_ID",
            help="The account to extend, by user id.",
        )
        target.add_argument(
            "--subscription",
            dest="subscription",
            metavar="STRIPE_SUBSCRIPTION_ID",
            help="The account to extend, by provider subscription id.",
        )
        parser.add_argument(
            "--days", type=int, required=True, help="How many days of comp time."
        )
        parser.add_argument(
            "--reason",
            required=True,
            help="Why. Recorded on the row — this is the audit.",
        )
        parser.add_argument(
            "--plan",
            default=None,
            help="Plan to entitle to. Default: the subscription's own plan.",
        )
        parser.add_argument(
            "--actor",
            default=None,
            help="Who is granting it. Default: the shell user.",
        )
        parser.add_argument(
            "--stack",
            action="store_true",
            help=(
                "Append to comp time this subscription already has. Without "
                "it, an existing window is an error rather than a silent "
                "extension."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the comp period. Without it, nothing is written.",
        )

    def _subscription(self, options) -> Subscription:
        if options.get("user"):
            sub = Subscription.objects.filter(user_id=options["user"]).first()
            if sub is None:
                raise CommandError(
                    f"no subscription row for user {options['user']} — a comp "
                    f"period extends an account that has one (the HTTP "
                    f"surface creates it on first read)."
                )
            return sub
        sub = Subscription.objects.filter(
            stripe_subscription_id=options["subscription"]
        ).first()
        if sub is None:
            raise CommandError(
                f"no local subscription carries provider id "
                f"{options['subscription']!r}"
            )
        return sub

    def handle(self, *args, **options):
        sub = self._subscription(options)
        actor = options.get("actor") or getpass.getuser()
        try:
            preview = services.extend_subscription(
                subscription=sub,
                days=options["days"],
                reason=options["reason"],
                actor=actor,
                plan=options.get("plan"),
                stack=bool(options.get("stack")),
                apply=bool(options.get("apply")),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        verb = "granted" if preview.applied else "would grant"
        self.stdout.write(
            f"{verb} {preview.days} day(s) of {preview.plan} to subscription "
            f"{preview.subscription_id}"
        )
        self.stdout.write(
            f"    window: {preview.starts_at.isoformat()} "
            f"-> {preview.ends_at.isoformat()}"
        )
        self.stdout.write(f"    reason: {preview.reason}")
        self.stdout.write(f"    by:     {preview.granted_by}")
        if preview.stacked_on:
            self.stdout.write(
                f"    stacked on the comp period {preview.stacked_on}, which "
                f"is why it starts when that one ends"
            )
        if preview.applied:
            self.stdout.write(
                self.style.SUCCESS(f"comp period {preview.comp_period_id} written")
            )
        else:
            self.stdout.write(
                self.style.WARNING("dry run — nothing was written (pass --apply)")
            )
