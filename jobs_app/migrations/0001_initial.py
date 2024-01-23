# Generated by Django 5.0.1 on 2024-01-23 14:33

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Group',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('group', models.CharField(max_length=100000)),
                ('threshold_value', models.IntegerField()),
                ('num_results', models.IntegerField()),
            ],
        ),
        migrations.CreateModel(
            name='Jobs',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('url', models.CharField(max_length=1000)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='jobs_app.group')),
            ],
        ),
        migrations.CreateModel(
            name='Queries',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('query', models.CharField(max_length=100000)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='jobs_app.group')),
            ],
        ),
        migrations.CreateModel(
            name='RequiredData',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('word', models.CharField(max_length=100000)),
                ('score', models.IntegerField()),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='jobs_app.group')),
            ],
        ),
    ]
