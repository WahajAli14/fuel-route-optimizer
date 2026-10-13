from django.db import models, transaction


class CityCoord(models.Model):
    """Coordinates for a US city, keyed by state + normalized name.

    Filled from the Census Gazetteer by `load_places`, plus one-time Nominatim
    lookups (or recorded misses) for station cities the Census files don't list.
    Used to place stations and to geocode request start/finish locally.
    """

    SOURCE_CENSUS_PLACE = 'census_place'
    SOURCE_CENSUS_COUSUB = 'census_cousub'
    SOURCE_NOMINATIM = 'nominatim'
    SOURCE_NOT_FOUND = 'not_found'
    SOURCE_CHOICES = [
        (SOURCE_CENSUS_PLACE, 'Census places'),
        (SOURCE_CENSUS_COUSUB, 'Census county subdivisions'),
        (SOURCE_NOMINATIM, 'Nominatim'),
        (SOURCE_NOT_FOUND, 'Not found'),
    ]

    state = models.CharField(max_length=2)
    key = models.CharField(max_length=120, help_text='normalize_city(name)')
    name = models.CharField(max_length=120)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    # Tie-breakers when spacing variants collide ("de forest" vs "deforest").
    priority = models.PositiveSmallIntegerField(default=0)
    land_area = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['state', 'key'], name='unique_city_per_state'),
        ]
        ordering = ['state', 'name']

    def __str__(self):
        return f'{self.name}, {self.state}'


class FuelStation(models.Model):
    """One truck stop from the fuel price CSV, after merging duplicate rows."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()
    # Mean of the duplicate rows' prices; min/max keep the spread visible.
    price = models.DecimalField(max_digits=8, decimal_places=4)
    min_price = models.DecimalField(max_digits=8, decimal_places=4)
    max_price = models.DecimalField(max_digits=8, decimal_places=4)
    row_count = models.PositiveSmallIntegerField(default=1, help_text='CSV rows merged into this station')
    # City-level coordinates (the CSV has no lat/lng).
    lat = models.FloatField()
    lng = models.FloatField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['state', 'city'])]
        ordering = ['state', 'city', 'name']

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'


class DataVersion(models.Model):
    """Single row whose version increments on every fuel price load.

    Cached route responses and the in-memory station index are keyed on it,
    so a reload invalidates both automatically.
    """

    version = models.PositiveIntegerField(default=0)
    loaded_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def current(cls):
        row = cls.objects.filter(pk=1).first()
        return row.version if row else 0

    @classmethod
    def bump(cls):
        from django.utils import timezone

        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(pk=1)
            row.version += 1
            row.loaded_at = timezone.now()
            row.save()
        return row.version

    def __str__(self):
        return f'v{self.version}'
