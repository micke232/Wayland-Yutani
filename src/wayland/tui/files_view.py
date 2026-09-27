from .panels import trading_rows


def workspace_root(thread):
    return thread.get("cwd", "")


class FilesView:
    def __init__(self):
        self.locations, self.hits = {}, {}
        self.rows = []
        self.index = self.scroll = 0
        self.picker = self.detail = None

    def visible(self, ui, height, width):
        from .ui import wrap

        self.rows = trading_rows(ui.data, "files", width, wrap)
        self.scroll = min(self.scroll, max(0, len(self.rows) - height))
        return [
            {**line, "source_index": index}
            for index, line in enumerate(self.rows[self.scroll : self.scroll + height], self.scroll)
        ]

    def move(self, delta, height):
        self.scroll = max(0, min(max(0, len(self.rows) - height), self.scroll + delta))

    def activate(self, *args, **kwargs):
        pass
