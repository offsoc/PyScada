# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2018-09-12 13:40
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hmi', '0013_widget_update_20180912_1315'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='widget',
            name='chart',
        ),
        migrations.RemoveField(
            model_name='widget',
            name='control_panel',
        ),
        migrations.RemoveField(
            model_name='widget',
            name='custom_html_panel',
        ),
        migrations.RemoveField(
            model_name='widget',
            name='process_flow_diagram',
        ),
    ]
