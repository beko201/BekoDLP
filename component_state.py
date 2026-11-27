"""Explicit compatibility aliases for fields owned by a runtime record."""


class RuntimeField:
    """Expose a declared runtime field through an existing facade attribute.

    Unlike a catch-all attribute proxy, only fields listed on the facade are
    forwarded. Components use the typed runtime directly.
    """

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return getattr(instance.runtime, self.name)

    def __set__(self, instance, value):
        setattr(instance.runtime, self.name, value)
