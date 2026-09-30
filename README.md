# Acceptance Test Failure: `InvalidQuantity`

## Target

| Field      | Value                                |
|------------|--------------------------------------|
| Repository | `cosmicpython/code`                  |
| Commit     | `14c8479`                            |
| Branch     | `master`                             |

## Summary

`test_rejects_non_positive_quantity` fails for both parametrized inputs (`0` and `-5`). The test expects `allocation.service_layer.handlers` to define an `InvalidQuantity` exception, but it does not exist.

| Test                                       | Result | Purpose                                                        |
|--------------------------------------------|--------|----------------------------------------------------------------|
| `test_rejects_non_positive_quantity[0]`    | Failed | Rejects a quantity of zero                                     |
| `test_rejects_non_positive_quantity[-5]`   | Failed | Rejects a negative quantity                                    |
| `test_positive_quantity_still_allocates`   | Passed | Checks that normal allocations still work                      |

**Total:** 2 failed, 1 passed in 0.20s

**Error:**

```text
AttributeError: module 'allocation.service_layer.handlers' has no attribute 'InvalidQuantity'
```

**Location:** `acceptance/test_acceptance_invalid_quantity.py:67`

## Full Test Output

```text
============================================== FAILURES ==============================================
_______________________________ test_rejects_non_positive_quantity[0] ________________________________

qty = 0

    @pytest.mark.parametrize("qty", [0, -5])
    def test_rejects_non_positive_quantity(qty):
        bus, notifs = make_bus()

>       with pytest.raises(handlers.InvalidQuantity):
                           ^^^^^^^^^^^^^^^^^^^^^^^^
E       AttributeError: module 'allocation.service_layer.handlers' has no attribute 'InvalidQuantity'

../../acceptance/test_acceptance_invalid_quantity.py:67: AttributeError
_______________________________ test_rejects_non_positive_quantity[-5] _______________________________

qty = -5

    @pytest.mark.parametrize("qty", [0, -5])
    def test_rejects_non_positive_quantity(qty):
        bus, notifs = make_bus()

>       with pytest.raises(handlers.InvalidQuantity):
                           ^^^^^^^^^^^^^^^^^^^^^^^^
E       AttributeError: module 'allocation.service_layer.handlers' has no attribute 'InvalidQuantity'

../../acceptance/test_acceptance_invalid_quantity.py:67: AttributeError
====================================== short test summary info =======================================
FAILED ../../acceptance/test_acceptance_invalid_quantity.py::test_rejects_non_positive_quantity[0] - AttributeError: module 'allocation.service_layer.handlers' has no attribute 'InvalidQuantity'
FAILED ../../acceptance/test_acceptance_invalid_quantity.py::test_rejects_non_positive_quantity[-5] - AttributeError: module 'allocation.service_layer.handlers' has no attribute 'InvalidQuantity'
2 failed, 1 passed in 0.20s
```