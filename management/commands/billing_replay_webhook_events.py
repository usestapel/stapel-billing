"""Re-run stored provider webhook events through the live handler path.

A webhook delivery that failed on a defect the deployment has since fixed
is not self-healing. The provider retries for about three days, with
backoff, and then stops for good — after that the stored
``StripeWebhookEvent`` row is the only copy of an event that was never
applied, and nothing will ever ask for it again. The local state simply
stays missing whatever that payload carried: a period, a status, a grant.

    manage.py billing_replay_webhook_events --unprocessed --dry-run
    manage.py billing_replay_webhook_events --unprocessed
    manage.py billing_replay_webhook_events --event evt_...

The replay is sanctioned rather than clever: it runs the SAME code live
delivery runs (``services.apply_stored_event`` — the lock on the
idempotency claim, the handler registry, the stale mark, the processed
mark, one atomic block), which is also why it cannot double-grant. The
signature was verified when the row was written; grants are claimed once
through ``ProviderGrant``; and the provider-time guard records a payload
older than the state it would overwrite as ``ignored_stale`` instead of
applying it. An event that already has ``processed_at`` is a no-op.

``--dry-run`` lists what would run and runs no handler — it deliberately
does NOT execute-and-roll-back, because a handler's reach is not only the
database and a rehearsal that sends a letter is not a rehearsal.
"""

from django.core.management.base import BaseCommand, CommandError

from ... import services


class Command(BaseCommand):
    help = (
        "Re-run stored Stripe webhook events through the same handler path "
        "live delivery uses (idempotent; a stale payload is ignored, never "
        "applied)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--event",
            action="append",
            dest="events",
            metavar="STRIPE_EVENT_ID",
            help="Replay this provider event id. Repeatable.",
        )
        parser.add_argument(
            "--unprocessed",
            action="store_true",
            help=(
                "Replay every event the delivery path never finished "
                "(no processed_at) — what billing_invariants counts as "
                "webhook_unprocessed."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be replayed; run no handler.",
        )

    def handle(self, *args, **options):
        event_ids = options.get("events")
        unprocessed = bool(options.get("unprocessed"))
        dry_run = bool(options.get("dry_run"))
        if not event_ids and not unprocessed:
            # Replaying "everything ever received" is not a thing anyone
            # means, and a default that re-runs the whole log is the kind
            # of default a tired operator finds out about afterwards.
            raise CommandError(
                "nothing selected: pass --event <id> (repeatable) or "
                "--unprocessed."
            )

        results = services.replay_webhook_events(
            event_ids=event_ids, unprocessed=unprocessed, dry_run=dry_run
        )
        if not results:
            self.stdout.write("no matching webhook events")
            return

        counts: dict[str, int] = {}
        for result in results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
            head = f"{result.stripe_event_id}  {result.event_type}"
            if result.received_at is not None:
                head += f"  received {result.received_at.isoformat()}"
            if result.outcome == "error":
                self.stdout.write(
                    self.style.ERROR(f"{head}\n    FAILED AGAIN: {result.error}")
                )
                continue
            if result.outcome == services.EVENT_DUPLICATE:
                self.stdout.write(f"{head}\n    already processed — no-op")
                continue
            if result.outcome == services.EVENT_IGNORED_STALE:
                self.stdout.write(
                    self.style.WARNING(
                        f"{head}\n    ignored_stale: the payload is OLDER than "
                        f"the state already applied, so nothing was written"
                    )
                )
                continue
            if dry_run:
                self.stdout.write(f"{head}\n    would replay")
            else:
                self.stdout.write(self.style.SUCCESS(f"{head}\n    processed"))
            if result.previous_error:
                self.stdout.write(f"    previous error: {result.previous_error}")

        summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        line = f"{len(results)} event(s): {summary}"
        if dry_run:
            line += "  (dry run — no handler ran)"
        self.stdout.write(self.style.SUCCESS(line))
        if counts.get("error"):
            # A replay that failed again has not fixed anything, and the
            # row still carries an error. Exit non-zero so a runbook step
            # cannot read this as done.
            raise SystemExit(1)
