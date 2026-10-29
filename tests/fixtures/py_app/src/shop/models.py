from dataclasses import dataclass, field

from .utils import money


@dataclass
class Order:
    """A customer order."""

    id: int
    status: str = "open"
    lines: list = field(default_factory=list)

    def cancel(self) -> None:
        """Cancel the order unless it was shipped."""
        if self.status == "shipped":
            raise ValueError("already shipped")
        self.status = "cancelled"

    def total(self) -> float:
        return money.round_money(sum(line.price for line in self.lines))
