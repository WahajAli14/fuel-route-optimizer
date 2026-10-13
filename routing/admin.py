from django.contrib import admin

from .models import CityCoord, DataVersion, FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'price', 'min_price', 'max_price', 'row_count')
    list_filter = ('state',)
    search_fields = ('opis_id', 'name', 'city', 'address')
    ordering = ('price',)


@admin.register(CityCoord)
class CityCoordAdmin(admin.ModelAdmin):
    list_display = ('name', 'state', 'lat', 'lng', 'source')
    list_filter = ('source', 'state')
    search_fields = ('name', 'key')


@admin.register(DataVersion)
class DataVersionAdmin(admin.ModelAdmin):
    list_display = ('version', 'loaded_at')
