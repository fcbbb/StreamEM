# Prompt source

Runtime prompts are defined only in [`multi-layer/`](multi-layer/). The Python
loader in [`__init__.py`](__init__.py) keeps the historical exported constants,
but all constants resolve to the canonical files under that directory.

`multi-layer/common/` contains the single copies of non-layered tasks:

- cutting;
- anchor extraction;
- community topic partitioning;
- topic-owner routing.

`multi-layer/templates/` contains one memory extraction template and one memory
fusion template. The loader supplies the input mode (`segments`, `memories`, or
`provisional_l1`) and the target level at runtime.

`multi-layer/levels/L1.txt`, `L2.txt`, and `L3.txt` contain only the rules that
actually differ by memory level. They are appended to the shared memory template
by the loader; they are not copies of the complete task prompt.

The old names `memory_extraction_from_memories`, `memory_fusion_from_l1`, and
`community_purification` are compatibility names only. They do not represent
separate prompt sources.

The memory output contracts are enforced by the calling code. Provenance IDs,
stable item IDs, source coverage, and level transitions remain code-owned rather
than being freely rewritten by the model.
