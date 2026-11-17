"""All use cases, grouped. The controllers (bots, webhooks, scheduler) talk to this and nothing deeper."""
from dataclasses import dataclass

from .jobs import JobUseCases
from .members import MemberUseCases
from .money import MoneyUseCases
from .ports import Deps
from .registration import RegistrationUseCases
from .teams import TeamUseCases
from .users import UserUseCases
from .web import WebUseCases


@dataclass(frozen=True)
class Services:
    users: UserUseCases
    registration: RegistrationUseCases
    teams: TeamUseCases
    members: MemberUseCases
    money: MoneyUseCases
    jobs: JobUseCases
    web: WebUseCases


def build_services(d: Deps) -> Services:
    return Services(users=UserUseCases(d), registration=RegistrationUseCases(d), teams=TeamUseCases(d),
                    members=MemberUseCases(d), money=MoneyUseCases(d), jobs=JobUseCases(d),
                    web=WebUseCases(d))
