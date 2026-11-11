"""Tests for Phase 6 (Advanced Memory & Learning)."""
import time

from vexinter import Vexinter
from vexinter.core.memory import LessonExtractor, MemoryDecay, MemoryIndex
from vexinter.core.tools import (
    Observation,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolAdapter,
    ToolSpec,
)


class DummyAdapter(ToolAdapter):
    def __init__(self, system: str = "sys"):
        self.system = system

    def tools(self) -> dict[str, ToolSpec]:
        return {
            "write": ToolSpec(
                system=self.system,
                name="write",
                description="Write data",
                input_schema={"type": "object"},
                operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE
            )
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        return OperationResult(ok=True, data={"written": True})

    def observe(self, query: dict) -> Observation:
        return Observation(data={"value": "ok"}, observed_at=None, source=self.system, consistency="strong")


def test_lesson_extractor():
    """Prueba el extractor de lecciones."""
    extractor = LessonExtractor(min_confidence=0.5)
    
    # Episodio exitoso
    episode = {
        "task_id": "t1",
        "objective": "Crear cliente y factura",
        "tags": {"agent": "ag1"},
        "tools_used": ["crm.create_customer", "billing.create_invoice"]
    }
    verdict = {"status": "met", "event_id": "evt1"}
    
    lessons = extractor.extract_from_episode(episode, verdict)
    assert len(lessons) >= 1
    lesson = lessons[0]
    assert "crm.create_customer" in lesson.pattern
    assert lesson.confidence > 0.5
    
    # Apply lesson successfully
    extractor.apply_lesson(lesson.id, True)
    assert lesson.application_count == 1
    assert lesson.success_count == 1
    assert lesson.confidence == 1.0
    
    # Obtener lecciones
    all_lessons = extractor.get_lessons()
    assert len(all_lessons) >= 1
    
    print("LessonExtractor test passed!")


def test_memory_index():
    """Tests the memory index."""
    index = MemoryIndex()
    
    index.add("ep1", "Crear cliente en CRM y luego factura en billing", {"type": "episode", "task_id": "t1"})
    index.add("ep2", "Crear factura directamente en billing", {"type": "episode", "task_id": "t2"})
    index.add("ep3", "Actualizar cliente en CRM", {"type": "episode", "task_id": "t3"})
    
    # Buscar similar
    results = index.search("crear cliente crm", top_k=5)
    assert len(results) >= 1
    assert "Crear cliente en CRM" in results[0]["text"]
    
    # Eliminar
    assert index.remove("ep1")
    results = index.search("crear cliente crm", top_k=5)
    # ep3 also matches (has "cliente" and "CRM"), but ep1 is gone now
    assert not any(r["id"] == "ep1" for r in results)
    
    print("MemoryIndex test passed!")


def test_memory_decay():
    """Prueba el decaimiento de memoria."""
    decay = MemoryDecay(ttl_seconds=86400, decay_factor=0.9)  # 1 day
    
    # No expirado
    assert not decay.should_expire(time.time() - 3600)  # 1 hora
    
    # Expirado
    assert decay.should_expire(time.time() - 172800)  # 2 days
    
    # Decaimiento de confianza
    conf = decay.apply_decay(1.0, 86400)  # 1 day
    assert conf == 0.9
    
    conf = decay.apply_decay(1.0, 172800)  # 2 days
    assert conf == 0.81
    
    print("MemoryDecay test passed!")


def test_advanced_memory_sdk():
    """Tests SDK integration with advanced memory."""
    adapter = DummyAdapter("sys")
    vex = Vexinter(embedded=True, adapters=[adapter])
    
    with vex.task(agent="ag1", objective="test advanced memory", 
                  condition={"op": "exists", "system": "sys", "query": {}}, input={}) as t:
        # Test get_lessons
        lessons = t.memory_get_lessons()
        assert isinstance(lessons, list)
        
        # Test search_similar
        results = t.memory_search_similar("test query", top_k=5)
        assert isinstance(results, list)
        
        # Test cross_task_patterns
        patterns = t.memory_get_cross_task_patterns()
        assert isinstance(patterns, dict)
        
        # Test cleanup
        result = t.memory_cleanup_expired()
        assert result is None  # void function
    
    print("Advanced Memory SDK integration test passed!")


if __name__ == "__main__":
    test_lesson_extractor()
    test_memory_index()
    test_memory_decay()
    test_advanced_memory_sdk()
    print("All Advanced Memory tests passed successfully!")

def test_cleanup_expired_drops_low_confidence_lessons():
    """M-7: cleanup_expired delega la limpieza de lecciones al extractor."""
    from vexinter.core.memory import MemoryService
    from vexinter.core.store import InMemoryEventStore
    svc = MemoryService(InMemoryEventStore())
    ext = svc._lesson_extractor
    lesson = ext.extract_from_episode(
        {"task_id": "t1", "objective": "o", "tags": {},
         "tools_used": ["crm.create_customer", "billing.create_invoice"]},
        {"status": "met", "event_id": "e1"})[0]
    lesson.confidence = 0.0
    ext._lessons[lesson.id] = lesson
    svc.cleanup_expired()
    assert lesson.id not in ext._lessons


def test_limpio_strips_bidi_controls():
    """B-2: caracteres bidireccionales no sobreviven a _limpio."""
    from vexinter.core.memory import _limpio
    assert "\u202e" not in _limpio("abc\u202edef")
    assert "\u2066" not in _limpio("a\u2066b\u2069c")
    assert _limpio("hola\u202emundo") == "hola mundo"
