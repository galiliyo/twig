"""
One-shot script: seed the WiseMapping mind-map with an initial subject tree.

Idempotent — safe to re-run. Existing branches and leaves are untouched;
only missing branches from the TREE list below are created.

Run:
    python bootstrap_tree.py
"""

import asyncio
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
load_dotenv()

from core.wisemapping import (
    WiseMapping,
    _assign_positions,
    _find_or_create_path,
)


# Each tuple is a path from the central node; missing levels are created.
TREE: list[list[str]] = [
    ["AI & ML", "Models & Capabilities"],
    ["AI & ML", "Industry & Companies"],
    ["AI & ML", "Use Cases & Workflows"],
    ["AI & ML", "Risks & Ethics"],

    ["Software Engineering", "Languages & Tools"],
    ["Software Engineering", "Architecture & Patterns"],
    ["Software Engineering", "DevOps & Infra"],
    ["Software Engineering", "Career & Practice"],

    ["Web Development"],

    ["Product & Design", "UX & Interaction"],
    ["Product & Design", "Product Strategy"],

    ["Business & Economics", "Tech Industry"],
    ["Business & Economics", "Macro & Markets"],
    ["Business & Economics", "Startups & Founders"],

    ["Personal Growth", "Productivity & Tools"],
    ["Personal Growth", "Health & Fitness"],
    ["Personal Growth", "Learning & Skills"],

    ["Ideas & Projects"],

    ["Reminders"],
]


async def main() -> None:
    wm = WiseMapping()
    await wm.login()

    xml_text = await wm._fetch_xml()
    root = ET.fromstring(xml_text)

    created = 0
    skipped = 0
    for path in TREE:
        # Walk the existing tree; count how many path components were missing
        central = root.find(".//topic[@central='true']") or root.find(".//topic")
        node = central
        path_existed = True
        for label in path:
            child = next(
                (c for c in node if c.tag == "topic" and c.get("text") == label),
                None,
            )
            if child is None:
                path_existed = False
                break
            node = child

        # Idempotent create
        _find_or_create_path(root, path)

        if path_existed:
            skipped += 1
            print(f"  [exists]  {' > '.join(path)}")
        else:
            created += 1
            print(f"  [created] {' > '.join(path)}")

    _assign_positions(root)
    await wm._save_xml(ET.tostring(root, encoding="unicode", xml_declaration=False))

    print(f"\nDone — {created} created, {skipped} already existed")
    await wm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
