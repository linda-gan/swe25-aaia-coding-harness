"""Acceptance check: allocating a non-positive quantity must be rejected."""
import pytest
from allocation.domain import commands
from allocation.service_layer import handlers
from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.service_layer import unit_of_work


class FakeRepository(repository.AbstractRepository):
    def __init__(self):
        super().__init__()
        self._products = set()

    def _add(self, product):
        self._products.add(product)

    def _get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref):
        return next(
            (p for p in self._products for b in p.batches if b.reference == batchref),
            None,
        )


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository()

    def _commit(self):
        pass

    def rollback(self):
        pass


class FakeNotifications(notifications.AbstractNotifications):
    def __init__(self):
        self.sent = {}

    def send(self, destination, message):
        self.sent.setdefault(destination, []).append(message)


def make_bus():
    notifs = FakeNotifications()
    bus = bootstrap.bootstrap(
        start_orm=False,
        uow=FakeUnitOfWork(),
        notifications=notifs,
        publish=lambda *args: None,
    )
    bus.handle(commands.CreateBatch("b1", "SMALL-TABLE", 10, None))
    return bus, notifs


@pytest.mark.parametrize("qty", [0, -5])
def test_rejects_non_positive_quantity(qty):
    bus, notifs = make_bus()

    with pytest.raises(handlers.InvalidQuantity):
        bus.handle(commands.Allocate("o1", "SMALL-TABLE", qty))

    [batch] = bus.uow.products.get("SMALL-TABLE").batches
    assert batch.available_quantity == 10          # stock unchanged
    assert notifs.sent == {}                       # no out-of-stock email


def test_positive_quantity_still_allocates():
    bus, _ = make_bus()
    bus.handle(commands.Allocate("o1", "SMALL-TABLE", 3))
    [batch] = bus.uow.products.get("SMALL-TABLE").batches
    assert batch.available_quantity == 7