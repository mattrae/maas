# Generated by Django 2.2.12 on 2020-09-21 16:03

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0216_remove_skip_bmc_config_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationdismissal",
            name="created",
            field=models.DateTimeField(
                default=django.utils.timezone.now, editable=False
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="notificationdismissal",
            name="updated",
            field=models.DateTimeField(
                default=django.utils.timezone.now, editable=False
            ),
            preserve_default=False,
        ),
    ]
