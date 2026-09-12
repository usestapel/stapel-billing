"""Repair local subscription rows that drifted from the provider.

Run it after deploying a release that changes how a provider payload is
read — the drift it exists to close is invisible to every other signal
this service has. Webhooks that were received, processed and marked green
can still have written the wrong thing, and nothing retries an event the
provider already considers delivered.

    manage.py billing_reconcile_subscriptions --dry-run
    manage.py billing_reconcile_subscriptions

Always dry-run first: the output is the same ledger either way, and the
run that writes should not be the run that tells you what it will write.
"""

from django.core.management.base import BaseCommand

from ... import services


def _render(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Command(BaseCommand):
    help = (
        "Re-read each provider-backed subscription and repair status, "
        "billing period and cancellation state from the provider."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change; write nothing.",
        )
        parser.add_argument(
            "--subscription",
            action="append",
            dest="subscriptions",
            metavar="STRIPE_SUBSCRIPTION_ID",
            help=(
                "Limit to this provider subscription id. Repeatable. "
                "Default: every row that has one."
            ),
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        results = services.reconcile_subscriptions(
            dry_run=dry_run,
            stripe_subscription_ids=options.get("subscriptions"),
        )
        if not results:
            self.stdout.write(
                "no provider-backed subscriptions to reconcile "
                "(free-plan rows have nothing to ask the provider about)"
            )
            return

        changed = failed = missing = 0
        for result in results:
            head = f"{result.stripe_subscription_id}  (row {result.subscription_id})"
            if result.error:
                failed += 1
                self.stdout.write(self.style.ERROR(f"{head}  ERROR {result.error}"))
                continue
            if result.missing:
                missing += 1
                self.stdout.write(
                    self.style.WARNING(f"{head}  GONE at the provider — untouched")
                )
                continue
            if not result.changed:
                self.stdout.write(f"{head}  ok")
                continue
            changed += 1
            verb = "would fix" if dry_run else "fixed"
            self.stdout.write(self.style.WARNING(f"{head}  {verb}:"))
            for field in result.changed:
                self.stdout.write(
                    f"    {field}: {_render(result.before[field])} "
                    f"-> {_render(result.after[field])}"
                )

        summary = (
            f"{len(results)} examined, {changed} "
            f"{'drifted' if dry_run else 'repaired'}, "
            f"{missing} gone, {failed} unreadable"
        )
        if dry_run:
            summary += "  (dry run — nothing was written)"
        self.stdout.write(self.style.SUCCESS(summary))
        if failed:
            # A sweep that could not read part of the fleet has not proved
            # that part clean; a non-zero exit is what stops a deploy
            # script from treating it as done.
            raise SystemExit(1)
