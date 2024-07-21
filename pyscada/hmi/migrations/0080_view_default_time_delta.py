# Generated by Django 5.0.3 on 2024-07-02 07:53

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hmi", "0079_displayvalueoption_from_timestamp_offset"),
    ]

    operations = [
        migrations.AddField(
            model_name="view",
            name="default_time_delta",
            field=models.DurationField(default=datetime.timedelta(seconds=7200)),
        ),
    ]