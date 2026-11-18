"""Use cases: contacts of opposing teams: a name, a phone number (the usual way to reach them) and a free
note (fb, zalo, contact person… as written). Everyone in the team bot can look them up, to find an opponent
and call them; only managers add, change or delete."""
from ..domain.entities import Contact, Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import fold
from .access import get_team, manager_team
from .people import audit
from .ports import Deps, UnitOfWork

LIMITS = {"name": 100, "phone": 10, "note": 500}
MAX_AT_ONCE = 60


def same(name: str | None) -> str:
    """Two contact names are the same team when they match ignoring accents, case and spaces ("KC United" = "KCUnited")."""
    return "".join(fold(name or "").split())


def _clean(field: str, v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise invalid()
    if field == "phone" and v and not (v.isdigit() and len(v) == 10 and v.startswith("0")):
        raise invalid()  # 10 digits, as the bot reads it from what people type
    v = " ".join(v.split())
    if len(v) > LIMITS[field]:
        raise invalid()
    return v or None


def contact_dict(c: Contact) -> dict:
    return {"id": c.id, "name": c.name, "phone": c.phone, "note": c.note}


def _by_name(uow: UnitOfWork, team: Team) -> dict[str, Contact]:
    return {same(c.name): c for c in uow.contacts.of_team(team.id)}


class ContactUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _contact(self, uow, team: Team, contact_id) -> Contact:
        c = uow.contacts.get(contact_id) if isinstance(contact_id, int) else None
        if not c or c.team_id != team.id:
            raise DomainError(404, "not_found")
        return c

    def search(self, team_id: int, actor: int, query: str | None = None) -> list[dict]:
        """All contacts A–Z, or those whose name, phone or note has the words (accents ignored). Anyone."""
        with self.d.uow() as uow:
            team = get_team(uow, team_id)
            items = sorted(uow.contacts.of_team(team.id), key=lambda c: fold(c.name))
            if query:
                words = fold(query).split()
                items = [c for c in items if all(w in fold(f"{c.name} {c.phone or ''} {c.note or ''}") for w in words)]
            return [contact_dict(c) for c in items]

    def get(self, team_id: int, actor: int, contact_id: int) -> dict:
        """The contact and its matches: how many played and cancelled, the last one, the next one. Anyone."""
        with self.d.uow() as uow:
            team = get_team(uow, team_id)
            c = self._contact(uow, team, contact_id)
            today = self.d.clock.local_now().date()
            mine = [m for m in uow.matches.all_of_team(team.id) if same(m.opponent) == same(c.name)]
            played = [m for m in mine if m.day < today and m.status == "scheduled"]
            ahead = [m for m in mine if m.day >= today and m.status == "scheduled"]
            return {**contact_dict(c), "played": len(played),
                    "cancelled": sum(m.status == "cancelled" for m in mine),
                    "last": played[-1].day.isoformat() if played else None,
                    "next": ahead[0].day.isoformat() if ahead else None}

    def preview(self, team_id: int, actor: int, items: list) -> list[dict]:
        """What saving these would do: each item marked new or an update of a contact with the same name."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            have = _by_name(uow, team)
            return [{**it, "update": same(it.get("name")) in have} for it in self._valid(items)]

    @staticmethod
    def _valid(items) -> list[dict]:
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_AT_ONCE:
            raise invalid()
        out = []
        for it in items:
            if not isinstance(it, dict) or not _clean("name", it.get("name")):
                raise invalid()
            out.append({"name": _clean("name", it["name"]), "phone": _clean("phone", it.get("phone")),
                        "note": _clean("note", it.get("note"))})
        return out

    def save_many(self, team_id: int, actor: int, items: list) -> dict:
        """Add contacts; one with the same name (accents and case ignored) is updated instead. A missing phone
        or note keeps the one already saved."""
        items = self._valid(items)
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            have = _by_name(uow, team)
            added = updated = 0
            for it in items:
                c = have.get(same(it["name"]))
                if c is None:
                    c = Contact(team_id=team.id, name=it["name"], phone=it["phone"], note=it["note"],
                                created_at=self.d.clock.now())
                    uow.contacts.add(c)
                    have[same(c.name)] = c
                    added += 1
                else:
                    c.name, c.phone, c.note = it["name"], it["phone"] or c.phone, it["note"] or c.note
                    updated += 1
            audit(uow, self.d.clock, team.id, actor, "contact.save",
                  target=items[0]["name"] if len(items) == 1 else str(len(items)),
                  after={"added": added, "updated": updated})
            uow.commit()
            return {"added": added, "updated": updated}

    def update(self, team_id: int, actor: int, contact_id: int, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes or not set(changes) <= set(LIMITS):
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            c = self._contact(uow, team, contact_id)
            clean = {k: _clean(k, v) for k, v in changes.items()}
            if "name" in clean and not clean["name"]:
                raise invalid()
            for k, v in clean.items():
                setattr(c, k, v)
            audit(uow, self.d.clock, team.id, actor, "contact.update", target=c.name, after=clean)
            uow.commit()
            return contact_dict(c)

    def delete(self, team_id: int, actor: int, contact_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            c = self._contact(uow, team, contact_id)
            out = contact_dict(c)
            uow.contacts.remove(c)
            audit(uow, self.d.clock, team.id, actor, "contact.delete", target=c.name)
            uow.commit()
            return out
