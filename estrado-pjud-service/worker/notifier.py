# worker/notifier.py
import logging

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, supabase):
        self._sb = supabase

    async def notify_new_movements(self, case: dict, new_count: int):
        if new_count <= 0:
            return

        user_id = case.get("assigned_user_id")

        if not user_id:
            resp = (
                self._sb.from_("users")
                .select("id")
                .eq("law_firm_id", case["law_firm_id"])
                .eq("role", "owner")
                .eq("status", "active")
                .limit(1)
                .execute()
            )
            if not resp.data:
                logger.warning("No user to notify for case %s", case["id"])
                return
            user_id = resp.data[0]["id"]

        n = new_count
        case_number = case["case_number"]
        plural = "s" if n > 1 else ""

        self._sb.from_("notifications").insert({
            "law_firm_id": case["law_firm_id"],
            "user_id": user_id,
            "title": f"Causa {case_number} - {n} movimiento{plural} nuevo{plural}",
            "body": f"Se detectaron cambios en OJV para la causa {case_number}.",
            "notification_type": "new_movement",
            "reference_type": "case",
            "reference_id": case["id"],
            "link": f"/cases/{case['id']}",
        }).execute()

        logger.info("Notification sent for case %s (%d movements)", case["id"], n)
