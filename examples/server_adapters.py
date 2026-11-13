from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem


def adapters():
    return [CrmSystem(), BillingSystem(fail_first=1), NotifierSystem()]