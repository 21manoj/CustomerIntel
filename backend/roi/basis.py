"""
Basis labels for every dollar figure (design §3): measured | derived | assumed.

    money(48000.0, 'measured', chain=['measured: outcome node 690'])
    weakest('derived', 'assumed')     -> 'assumed'

A figure inherits the weakest link of its chain; measured and assumed are
never added — callers keep them as separate keys.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from roi import settings


def rank() -> List[str]:
    return list(settings.get('basis_rank'))


def weakest(*bases: str) -> str:
    order = rank()
    present = [b for b in bases if b in order]
    if not present:
        raise ValueError(f'no known basis among {bases!r}; known: {order}')
    return max(present, key=order.index)


def money(value: Optional[float], basis: str, chain: Optional[Iterable[str]] = None, note: Optional[str] = None) -> dict:
    """One labelled dollar figure. value None = not computable (the note says why)."""
    if basis not in rank():
        raise ValueError(f'unknown basis {basis!r}')
    out = {'value': round(float(value), 2) if value is not None else None, 'basis': basis,
           'basis_chain': list(chain or [f'{basis}'])}
    if note:
        out['note'] = note
    return out
