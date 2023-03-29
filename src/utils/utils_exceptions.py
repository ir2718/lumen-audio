class InvalidArgument(Exception):
    """Argument is invalid."""


class InvalidDataException(Exception):
    """Data is invalid."""


class InvalidRatios(Exception):
    """Invalid ratios."""


class UnsupportedOptimizer(ValueError):
    pass


class UnsupportedScheduler(ValueError):
    pass


class UnsupportedModel(ValueError):
    pass


class InvalidModuleStr(ValueError):
    """Invalid submodule string which isn't contained within the parent module."""