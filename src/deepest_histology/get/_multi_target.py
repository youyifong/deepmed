from pathlib import Path
from typing import Iterable, Iterator
from typing_extensions import Protocol
from ..types import Run


class MultiTargetBaseRunGetter(Protocol):
    """The signature of a run getter which can be modified by ``multi_target``."""
    def __call__(
            self, *args,
            project_dir: Path, target_label: str, **kwargs) \
            -> Iterator[Run]:
        ...


def multi_target(
    get: MultiTargetBaseRunGetter, *args,
    project_dir: Path, target_labels: Iterable[str], **kwargs) \
    -> Iterator[Run]:
    """Adapts a `RunGetter` into a multi-target one.

    Args:
        get:  The `RunGetter` to adapt; it has to take at least one keyword
            argument `target_label`.
        project_dir:  The directory to save the runs' results to.
        target_label:  The target labels to invoke `get` on.
        *args:  Additional arguments give to `get`.
        **kwargs:  Additional keyword arguments to give to `get`.

    Yields:
        The runs which would be yielded by `get` for each of the target labels,
        in the order of the target labels.  The run directories are prepended by
        a the name of the target label.
    """
    for target_label in target_labels:
        target_dir = project_dir/target_label
        target_dir.mkdir(parents=True, exist_ok=True)

        for run in get(*args, project_dir=target_dir, target_label=target_label, **kwargs):
            yield run