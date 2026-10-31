# Attribution and licensing

DrivePak Manager's Python, UI, tests, packaging and documentation are original
implementations under [MIT](LICENSE). Names of third-party products identify
compatibility; no affiliation or endorsement is implied.

Research references (downloaded separately, never bundled):

* Roger Taylor: MicroSD Drive Pak hardware, CoCoNet firmware and
  [Quick Docs](https://colorcomputerarchive.com/repo/Documents/Manuals/Hardware/MicroSD%20Drive%20Pak%20Quick%20Docs%20%28Roger%20Taylor%29.pdf).
* Guillaume Major: [DrivePak Explorer 1.00 source](https://colorcomputerarchive.com/repo/Programming/Source/DrivePak%20Explorer/DrivePak%20Explorer%201.00%20Source%20Code%20%28Guillaume%20Major%29.zip),
  2016–2017. Its header acknowledges Sidekick by Luis Antoniosi. All six archive
  members were inspected. No explicit reuse license accompanies that archive;
  **none of its code is incorporated, translated, or distributed here**. Labels
  and line numbers in FORMAT.md identify observations of the reference.
* The Color Computer Archive preserves those works and the reference images.
  Public download is not assumed to grant redistribution rights. Archived games,
  firmware, indexes, source and card images remain in the ignored research cache.
* Disk BASIC filesystem behavior was checked against the original disassembly
  and commentary in [Disk BASIC Unravelled II](https://colorcomputerarchive.com/coco/Documents/Books/Unravelled%20Series/disk-basic-unravelled.pdf)
  (Spectral Associates; revised Walter K. Zydhek), and independent byte fixtures.
* OS-9 RBF structure definitions were checked against the NitrOS-9 project's
  [RBF documentation](https://sourceforge.net/p/nitros9/wiki/Random_Block_File_Manager/).
  No implementation code from NitrOS-9 or ToolShed is reused.

Runtime dependency: PySide6/Shiboken and Qt for Python 6.10.0 are installed from
their separately distributed wheels. Qt/PySide components have their own
LGPLv3/GPLv3/commercial licensing and some Qt modules have additional terms;
consult the wheels' `dist-info/licenses`, `PySide6/Qt` license notices and the
[Qt licensing documentation](https://doc.qt.io/qtforpython-6/licenses.html).
This project's wheel and Linux archive do not embed Qt binaries. The helper
zipapp uses only the Python standard library. Python, pytest, pytest-qt,
setuptools, build and their dependencies retain their respective licenses.

The generated HELLO.BAS used by desktop smoke inspection is original text
created by this project; it is not included in new blank user images.
