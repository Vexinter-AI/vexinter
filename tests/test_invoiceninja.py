import json

import httpx
import pytest

from vexinter import Vexinter
from vexinter.core.events import EventType
from vexinter.core.tools import OperationClass, OperationRequest
from vexinter.invoiceninja import (
    IndeterminateOutcome,
    InvoiceNinjaBilling,
    InvoiceNinjaClient,
    InvoiceNinjaCrm,
    _customer,
    _invoice,
)


def _client(handler):
    return InvoiceNinjaClient("http://x", "t",
                              transport=httpx.MockTransport(handler))


def _resp(data, status=200, meta=None):
    body = {"data": data}
    if meta:
        body["meta"] = meta
    return httpx.Response(status, json=body)


def _inv(id_, status_id, client="C1", archived=0, deleted=False):
    return {"id": id_, "number": "0001", "status_id": status_id,
            "client_id": client, "amount": 100, "archived_at": archived,
            "is_deleted": deleted}


def _cli(id_, email="a@b.c", archived=0, deleted=False):
    return {"id": id_, "name": "N", "archived_at": archived,
            "is_deleted": deleted, "contacts": [{"email": email}]}


def _req(args):
    return OperationRequest(tool="t", args=args)


def test_the_adapters_declare_what_was_measured():
    crm = InvoiceNinjaCrm(_client(lambda r: _resp([])))
    billing = InvoiceNinjaBilling(_client(lambda r: _resp([])))
    ci = billing.tools()["create_invoice"]
    assert ci.supports_idempotency is False
    assert ci.operation_class == OperationClass.REVERSIBLE_WRITE
    assert billing.tools()["void_invoice"].supports_idempotency is True
    assert crm.tools()["create_customer"].supports_idempotency is True
    assert ci.compensation.tool == "billing.void_invoice"
    assert ci.compensation.args_from_result == {"invoice_id": "id"}
    assert ci.effect_probe.system == "billing"


def test_verified_and_unknown_status_mapping():
    assert _invoice(_inv("a", "1"))["status"] == "draft"
    assert _invoice(_inv("a", "2"))["status"] == "issued"
    assert _invoice(_inv("a", "5"))["status"] == "voided"
    assert _invoice(_inv("a", "4"))["status"] == "status_4"
    assert _invoice(_inv("a", "2", archived=5))["status"] == "archived"
    assert _customer(_cli("c"))["status"] == "active"
    assert _customer(_cli("c", archived=5))["status"] == "archived"


def test_a_possibly_truncated_list_is_unknown():
    q = {"type": "invoice", "where": {"customer_id": "C1"}}
    b = InvoiceNinjaBilling(_client(
        lambda r: _resp([_inv("a", "2")], meta={"pagination": {"total": 10}})))
    assert b.observe(q).consistency == "unknown"
    b2 = InvoiceNinjaBilling(_client(
        lambda r: _resp([_inv(str(i), "2") for i in range(20)])))
    assert b2.observe(q).consistency == "unknown"


def test_a_complete_observe_is_strong_and_filters_adapter_side():
    b = InvoiceNinjaBilling(_client(lambda r: _resp(
        [_inv("a", "2"), _inv("b", "5")], meta={"pagination": {"total": 2}})))
    obs = b.observe({"type": "invoice", "many": True,
                     "where": {"customer_id": "C1", "status": "issued"}})
    assert obs.consistency == "strong"
    assert [x["id"] for x in obs.data] == ["a"]


def test_a_cut_and_5xx_are_unknown_outcome_not_failure():
    def h5(r):
        return _resp([]) if r.method == "GET" else httpx.Response(503, json={})

    def hcut(r):
        raise httpx.ConnectError("no network")

    args = _req({"name": "N", "email": "x@y.z"})
    with pytest.raises(IndeterminateOutcome):
        InvoiceNinjaCrm(_client(h5)).invoke("create_customer", args)
    with pytest.raises(IndeterminateOutcome):
        InvoiceNinjaCrm(_client(hcut)).invoke("create_customer", args)


def test_a_4xx_is_a_definitive_failure():
    def h(r):
        return (_resp([]) if r.method == "GET"
                else httpx.Response(422, json={"message": "x"}))

    res = InvoiceNinjaCrm(_client(h)).invoke(
        "create_customer", _req({"name": "N", "email": "x@y.z"}))
    assert not res.ok


def test_a_created_but_not_issued_invoice_reports_error_with_the_draft():
    def h(r):
        if r.url.path.endswith("/invoices/bulk"):
            return httpx.Response(422, json={"message": "no"})
        return _resp(_inv("n1", "1"))

    res = InvoiceNinjaBilling(_client(h)).invoke(
        "create_invoice", _req({"customer_id": "C1", "amount": 100}))
    assert not res.ok and res.data["status"] == "draft"


def test_voiding_an_already_voided_invoice_does_not_void_again():
    calls = []

    def h(r):
        calls.append(r.method)
        return _resp(_inv("a", "5"))

    res = InvoiceNinjaBilling(_client(h)).invoke(
        "void_invoice", _req({"invoice_id": "a"}))
    assert res.ok and res.data["status"] == "voided"
    assert set(calls) == {"GET"}


def test_an_archived_client_with_that_email_is_not_reused():
    crm = InvoiceNinjaCrm(_client(
        lambda r: _resp([_cli("c1", "x@y.z", archived=123)])))
    res = crm.invoke("create_customer", _req({"name": "N", "email": "x@y.z"}))
    assert not res.ok and res.data["status"] == "archived"


def test_a_cut_leaves_the_invocation_without_return_in_the_log():
    def hcut(r):
        raise httpx.ConnectError("no network")

    vex = Vexinter(embedded=True, adapters=[InvoiceNinjaCrm(_client(hcut))])
    cond = {"op": "exists", "system": "crm",
            "query": {"type": "customer", "where": {}}}
    with vex.task(agent="a", objective="o", condition=cond, input={}) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.crm.create_customer({"name": "N", "email": "x@y.z"})
        tipos = [e.type for e in t.events()]
        assert EventType.TOOL_INVOKED in tipos
        assert EventType.TOOL_RETURNED not in tipos


def _void(status_id, archived=0):
    posts = []

    def h(r):
        if r.method == "GET":
            return _resp(_inv("a", status_id, archived=archived))
        accion = json.loads(r.content)["action"]
        posts.append(accion)
        if accion == "cancel":
            return _resp([_inv("a", "5")])
        return _resp([_inv("a", "1", archived=1)])

    res = InvoiceNinjaBilling(_client(h)).invoke(
        "void_invoice", _req({"invoice_id": "a"}))
    return res, posts


def test_voiding_a_draft_archives_it_because_it_cannot_be_cancelled():
    res, posts = _void("1")
    assert res.ok and posts == ["archive"]


def test_voiding_a_sent_invoice_cancels_it():
    res, posts = _void("2")
    assert res.ok and posts == ["cancel"] and res.data["status"] == "voided"


def test_voiding_an_already_archived_draft_does_not_repeat_the_action():
    res, posts = _void("1", archived=5)
    assert res.ok and posts == []


def test_retry_after_unissued_draft_is_blocked_by_the_probe():
    """The invoice was created as a draft but could not be issued (ok=False): a
    retry would duplicate the draft."""
    import pytest

    from vexinter.core.tools import EffectAlreadyApplied
    creadas = []

    def h(r):
        if r.url.path.endswith("/invoices/bulk"):
            return httpx.Response(422, json={"message": "no"})
        if r.method == "POST":
            creadas.append(_inv(f"n{len(creadas) + 1}", "1"))
            return _resp(creadas[-1])
        return _resp(list(creadas), meta={"pagination": {"total": len(creadas)}})

    vex = Vexinter(embedded=True, adapters=[InvoiceNinjaBilling(_client(h))])
    cond = {"op": "exists", "system": "billing",
            "query": {"type": "invoice", "where": {}}}
    with vex.task(agent="a", objective="o", condition=cond, input={}) as t:
        args = {"customer_id": "C1", "amount": 100}
        assert not t.tools.billing.create_invoice(args).ok
        with pytest.raises(EffectAlreadyApplied):
            t.tools.billing.create_invoice(args)
        assert len(creadas) == 1


def test_human_approval_is_chosen_when_creating_the_adapter():
    c = _client(lambda r: _resp([]))
    assert InvoiceNinjaBilling(c).tools()[
        "create_invoice"].retry_approval == "auto"
    assert InvoiceNinjaBilling(c, approval="human").tools()[
        "create_invoice"].retry_approval == "human"
    with pytest.raises(ValueError):
        InvoiceNinjaBilling(c, approval="quizas")


def test_the_once_per_execution_option_is_chosen_when_creating_the_adapter():
    c = _client(lambda r: _resp([]))
    assert InvoiceNinjaBilling(c).tools()[
        "create_invoice"].once_per_execution is False
    assert InvoiceNinjaBilling(c, once_per_execution=True).tools()[
        "create_invoice"].once_per_execution is True