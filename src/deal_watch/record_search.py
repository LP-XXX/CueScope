"""Immutable parameters shared by the activity and recognition readers."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RecordSearch:
    task: str = ""
    text: str = ""
    hours: int = 0

    def conditions(self):
        clauses, values = [], []
        if self.task:
            clauses.append("instr(lower(region_alias), lower(?)) > 0")
            values.append(self.task)
        if self.text:
            clauses.append("instr(lower(text), lower(?)) > 0")
            values.append(self.text)
        if self.hours:
            clauses.append("observed_at >= ?")
            values.append(time.time() - self.hours * 3600)
        return clauses, values
