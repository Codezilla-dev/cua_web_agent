"""Is the structural perception pass good enough to reason over?

Pure function over already-extracted data, so it stays reproducible — this is
what would later decide whether to pay for a vision tier. There is no vision
tier yet; on failure the caller logs and continues.
"""

from src.types import PerceptionCoverage, UIElement


def compute_coverage(
    elements: list[UIElement],
    empty_frames: list[str],
    min_interactive_elements: int,
    max_unnamed_ratio: float,
) -> PerceptionCoverage:
    """Judge whether perception produced a usable picture.

    A frame yielding zero elements usually means the a11y tree never loaded.
    `reason` lists every failure, not just the first.
    """
    interactive_count = len(elements)

    unnamed_count = sum(1 for element in elements if not element.name.strip())
    # No elements: the count check below already fails, so report 0.0.
    unnamed_ratio = (unnamed_count / interactive_count) if interactive_count else 0.0

    reasons: list[str] = []

    if interactive_count < min_interactive_elements:
        reasons.append(
            f"only {interactive_count} interactive elements found "
            f"(minimum {min_interactive_elements})"
        )

    if unnamed_ratio > max_unnamed_ratio:
        reasons.append(
            f"{unnamed_count}/{interactive_count} elements have no accessible name "
            f"({unnamed_ratio:.0%} > {max_unnamed_ratio:.0%} allowed)"
        )

    if empty_frames:
        reasons.append(f"frames yielded zero elements: {', '.join(empty_frames)}")

    return PerceptionCoverage(
        ok=not reasons,
        reason="; ".join(reasons) if reasons else None,
        interactive_count=interactive_count,
        unnamed_ratio=unnamed_ratio,
        empty_frames=empty_frames,
    )
