"""Acceptance check: batches can have an optional expiry date.

Uses only the public interface (commands through the message bus, the
read model) and its own test fakes, so it does not depend on how the
change is structured or on files the agent could edit.

Rules being checked:
- CreateBatch takes an optional expires date; without it nothing changes.
- A batch can be allocated up to and including its expiry date.
- An expired batch is skipped; with no other batch the product is out of stock.
- An expiry date earlier than the eta is rejected with InvalidExpiry,
  and the batch is not added.
- The expiry date is stored with the batch in the database.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import clear_mappers, sessionmaker

from allocation import bootstrap, views
from allocation.adapters import notifications, orm, repository
from allocation.domain import commands, model
from allocation.service_layer import handlers, unit_of_work

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


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
    return bus, notifs


def available(bus, sku):
    product = bus.uow.products.get(sku)
    return {b.reference: b.available_quantity for b in product.batches}


# ---- existing callers keep working ----------------------------------------

def test_batches_without_expiry_work_as_before():
    bus, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None))  # positional, as before
    bus.handle(commands.CreateBatch("b2", "LAMP", 10, eta=TOMORROW))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert available(bus, "LAMP") == {"b1": 7, "b2": 10}


def test_domain_objects_without_expiry_work_as_before():
    batch = model.Batch("b1", "LAMP", 10, None)
    product = model.Product("LAMP", [batch])
    assert product.allocate(model.OrderLine("o1", "LAMP", 4)) == "b1"
    assert batch.available_quantity == 6


# ---- expiry rules ---------------------------------------------------------

@pytest.mark.parametrize("expires", [TODAY, TOMORROW])
def test_batch_can_be_allocated_up_to_and_including_its_expiry_date(expires):
    bus, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None, expires=expires))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert available(bus, "LAMP") == {"b1": 7}


def test_expired_batch_is_skipped_and_another_batch_is_used():
    bus, notifs = make_bus()
    # The warehouse batch (no eta) would normally be preferred, but it expired.
    bus.handle(commands.CreateBatch("old", "LAMP", 10, None, expires=YESTERDAY))
    bus.handle(commands.CreateBatch("new", "LAMP", 10, TOMORROW))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert available(bus, "LAMP") == {"old": 10, "new": 7}
    assert notifs.sent == {}


def test_only_expired_stock_means_out_of_stock():
    bus, notifs = make_bus()
    bus.handle(commands.CreateBatch("old", "LAMP", 10, None, expires=YESTERDAY))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert available(bus, "LAMP") == {"old": 10}
    assert notifs.sent == {"stock@made.com": ["Out of stock for LAMP"]}


# ---- the error case -------------------------------------------------------

def test_expiry_before_eta_is_rejected_and_batch_is_not_added():
    bus, _ = make_bus()
    eta = TODAY + timedelta(days=10)
    with pytest.raises(handlers.InvalidExpiry):
        bus.handle(commands.CreateBatch("b1", "LAMP", 10, eta, expires=eta - timedelta(days=1)))
    product = bus.uow.products.get("LAMP")
    assert product is None or product.batches == []


def test_expiry_on_the_eta_is_accepted():
    bus, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, TOMORROW, expires=TOMORROW))
    assert available(bus, "LAMP") == {"b1": 10}


# ---- persistence ----------------------------------------------------------

@pytest.fixture
def sqlite_bus():
    engine = create_engine("sqlite:///:memory:")
    orm.metadata.create_all(engine)
    bus = bootstrap.bootstrap(
        start_orm=True,
        uow=unit_of_work.SqlAlchemyUnitOfWork(sessionmaker(bind=engine)),
        notifications=FakeNotifications(),
        publish=lambda *args: None,
    )
    yield bus
    clear_mappers()


def test_expiry_is_stored_with_the_batch(sqlite_bus):
    # Each command opens a new database session, so the Allocate command
    # works on batches loaded back from the database, not on the objects
    # that CreateBatch made. If the expiry date was not stored, the
    # expired warehouse batch would be chosen.
    sqlite_bus.handle(commands.CreateBatch("old", "LAMP", 10, None, expires=YESTERDAY))
    sqlite_bus.handle(commands.CreateBatch("new", "LAMP", 10, TOMORROW))
    sqlite_bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert views.allocations("o1", sqlite_bus.uow) == [{"sku": "LAMP", "batchref": "new"}]
