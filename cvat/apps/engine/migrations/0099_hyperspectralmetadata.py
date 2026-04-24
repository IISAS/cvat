from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0098_data_local_storage_backing_cs"),
    ]

    operations = [
        migrations.CreateModel(
            name="HyperspectralMetadata",
            fields=[
                (
                    "image",
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        primary_key=True,
                        related_name="hyperspectral",
                        serialize=False,
                        to="engine.image",
                    ),
                ),
                ("band_count", models.PositiveIntegerField()),
                ("lines", models.PositiveIntegerField()),
                ("samples", models.PositiveIntegerField()),
                ("interleave", models.CharField(max_length=3)),
                ("dtype", models.CharField(max_length=16)),
                ("default_r_band", models.PositiveIntegerField()),
                ("default_g_band", models.PositiveIntegerField()),
                ("default_b_band", models.PositiveIntegerField()),
                ("default_stretch", models.JSONField(blank=True, null=True)),
                ("wavelengths", models.JSONField(blank=True, null=True)),
                ("data_ignore_value", models.FloatField(blank=True, null=True)),
                ("crs_wkt", models.TextField(blank=True, null=True)),
                ("map_info", models.JSONField(blank=True, null=True)),
                ("data_file_path", models.CharField(max_length=1024)),
                ("hdr_file_path", models.CharField(max_length=1024)),
            ],
            options={
                "default_permissions": (),
            },
        ),
    ]
