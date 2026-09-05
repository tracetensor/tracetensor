"""Filesystem tools for the deep-research agent.

Deliberately a nested package rather than a flat module: a real LangGraph project
splits tools out from the graph, and a project laid out this way is what proved
`DockerEnvironment.copy_in` was skipping subdirectories.
"""

from tools.filesystem import list_files, read_file, write_file

__all__ = ["list_files", "read_file", "write_file"]
