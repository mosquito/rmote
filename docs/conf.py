project = "rmote"
copyright = "2024, rmote contributors"
author = "rmote contributors"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
    "myst_parser",
    "sphinxcontrib.mermaid",
    "sphinx_copybutton",
]

myst_enable_extensions = ["colon_fence"]

html_theme = "furo"
html_title = "rmote"
html_logo = "_static/logo.svg"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_theme_options = {
    "source_repository": "https://github.com/mosquito/rmote",
    "source_branch": "master",
    "source_directory": "docs/",
}

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "special-members": "__call__",
}
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True

viewcode_follow_imported_members = True
