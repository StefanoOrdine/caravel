from flask.ext.appbuilder import Model
from pydruid import client
from datetime import timedelta
from flask.ext.appbuilder.models.mixins import AuditMixin, FileColumn
from sqlalchemy import Column, Integer, String, ForeignKey, Text, Boolean, DateTime
from sqlalchemy.orm import relationship
from app import get_session
from dateutil.parser import parse

import logging
import json
import requests


class Cluster(Model, AuditMixin):
    __tablename__ = 'clusters'
    id = Column(Integer, primary_key=True)
    cluster_name = Column(String(256), unique=True)
    coordinator_host = Column(String(256))
    coordinator_port = Column(Integer)
    coordinator_endpoint = Column(String(256))
    broker_host = Column(String(256))
    broker_port = Column(Integer)
    broker_endpoint = Column(String(256))
    metadata_last_refreshed = Column(DateTime)

    def __repr__(self):
        return self.cluster_name

    def get_pydruid_client(self):
        cli = client.PyDruid(
            "http://{0}:{1}/".format(self.broker_host, self.broker_port),
            self.broker_endpoint)
        return cli

    def refresh_datasources(self):
        endpoint = (
            "http://{self.coordinator_host}:{self.coordinator_port}/"
            "{self.coordinator_endpoint}/datasources"
        ).format(self=self)
        datasources = json.loads(requests.get(endpoint).text)
        for datasource in datasources:
            #try:
                Datasource.sync_to_db(datasource, self)
            #except Exception as e:
            #    logging.exception(e)
            #    logging.error("Failed at syncing " + datasource)

class Datasource(Model, AuditMixin):
    __tablename__ = 'datasources'
    id = Column(Integer, primary_key=True)
    datasource_name = Column(String(256), unique=True)
    is_featured = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    description = Column(Text)
    default_endpoint = Column(Text)
    user_id = Column(Integer,
        ForeignKey('ab_user.id'))
    owner = relationship('User', backref='datasources', foreign_keys=[user_id])
    cluster_name = Column(Integer,
        ForeignKey('clusters.cluster_name'))
    cluster = relationship('Cluster', backref='datasources', foreign_keys=[cluster_name])

    @property
    def metrics_combo(self):
        return sorted(
            [(m.metric_name, m.verbose_name) for m in self.metrics],
            key=lambda x: x[1])

    def __repr__(self):
        return self.datasource_name

    @property
    def datasource_link(self):
        url = "/panoramix/datasource/{}/".format(self.datasource_name)
        return '<a href="{url}">{self.datasource_name}</a>'.format(**locals())

    def get_metric_obj(self, metric_name):
        return [
            m.json_obj for m in self.metrics
            if m.metric_name == metric_name
        ][0]

    def latest_metadata(self):
        client = self.cluster.get_pydruid_client()
        results = client.time_boundary(datasource=self.datasource_name)
        max_time = results[0]['result']['minTime']
        max_time = parse(max_time)
        intervals = (max_time - timedelta(seconds=1)).isoformat() + '/'
        intervals += (max_time + timedelta(seconds=1)).isoformat()
        segment_metadata = client.segment_metadata(
            datasource=self.datasource_name,
            intervals=intervals)
        if segment_metadata:
            return segment_metadata[-1]['columns']

    def generate_metrics(self):
        for col in self.columns:
            col.generate_metrics()

    @classmethod
    def sync_to_db(cls, name, cluster):
        session = get_session()
        datasource = session.query(cls).filter_by(datasource_name=name).first()
        if not datasource:
            datasource = cls(datasource_name=name)
            session.add(datasource)
        datasource.cluster = cluster

        cols = datasource.latest_metadata()
        if not cols:
            return
        for col in cols:
            col_obj = (
                session
                .query(Column)
                .filter_by(datasource_name=name, column_name=col)
                .first()
            )
            datatype = cols[col]['type']
            if not col_obj:
                col_obj = Column(datasource_name=name, column_name=col)
                session.add(col_obj)
            if datatype == "STRING":
                col_obj.groupby = True
                col_obj.filterable = True
            if col_obj:
                col_obj.type = cols[col]['type']
            col_obj.generate_metrics()
        #session.commit()

    @property
    def column_names(self):
        return sorted([c.column_name for c in self.columns])

    @property
    def groupby_column_names(self):
        return sorted([c.column_name for c in self.columns if c.groupby])

    @property
    def filterable_column_names(self):
        return sorted([c.column_name for c in self.columns if c.filterable])


class Metric(Model):
    __tablename__ = 'metrics'
    id = Column(Integer, primary_key=True)
    metric_name = Column(String(512))
    verbose_name = Column(String(1024))
    metric_type = Column(String(32))
    datasource_name = Column(
        String(256),
        ForeignKey('datasources.datasource_name'))
    datasource = relationship('Datasource', backref='metrics')
    json = Column(Text)
    description = Column(Text)

    @property
    def json_obj(self):
        try:
            obj = json.loads(self.json)
        except Exception as e:
            obj = {}
        return obj


class Column(Model, AuditMixin):
    __tablename__ = 'columns'
    id = Column(Integer, primary_key=True)
    datasource_name = Column(
        String(256),
        ForeignKey('datasources.datasource_name'))
    datasource = relationship('Datasource', backref='columns')
    column_name = Column(String(256))
    is_active = Column(Boolean, default=True)
    type = Column(String(32))
    groupby = Column(Boolean, default=False)
    count_distinct = Column(Boolean, default=False)
    sum = Column(Boolean, default=False)
    max = Column(Boolean, default=False)
    min = Column(Boolean, default=False)
    filterable = Column(Boolean, default=False)
    description = Column(Text)

    def __repr__(self):
        return self.column_name

    @property
    def isnum(self):
        return self.type in ('LONG', 'DOUBLE', 'FLOAT')

    def generate_metrics(self):
        M = Metric
        metrics = []
        metrics.append(Metric(
            metric_name='count',
            verbose_name='COUNT(*)',
            metric_type='count',
            json=json.dumps({'type': 'count', 'name': 'count'})
        ))
        # Somehow we need to reassign this for UDAFs
        corrected_type = 'DOUBLE' if self.type in ('DOUBLE', 'FLOAT') else 'self.type'

        if self.sum and self.isnum:
            mt = corrected_type.lower() + 'Sum'
            name='sum__' + self.column_name
            metrics.append(Metric(
                metric_name=name,
                metric_type='sum',
                verbose_name='SUM({})'.format(self.column_name),
                json=json.dumps({
                    'type': mt, 'name': name, 'fieldName': self.column_name})
            ))
        if self.min and self.isnum:
            mt = corrected_type.lower() + 'Min'
            name='min__' + self.column_name
            metrics.append(Metric(
                metric_name=name,
                metric_type='min',
                verbose_name='MIN({})'.format(self.column_name),
                json=json.dumps({
                    'type': mt, 'name': name, 'fieldName': self.column_name})
            ))
        if self.max and self.isnum:
            mt = corrected_type.lower() + 'Max'
            name='max__' + self.column_name
            metrics.append(Metric(
                metric_name=name,
                metric_type='max',
                verbose_name='MAX({})'.format(self.column_name),
                json=json.dumps({
                    'type': mt, 'name': name, 'fieldName': self.column_name})
            ))
        if self.count_distinct:
            mt = 'count_distinct'
            name='count_distinct__' + self.column_name
            metrics.append(Metric(
                metric_name=name,
                verbose_name='COUNT(DISTINCT {})'.format(self.column_name),
                metric_type='count_distinct',
                json=json.dumps({
                    'type': 'cardinality',
                    'name': name,
                    'fieldNames': [self.column_name]})
            ))
        session = get_session()
        for metric in metrics:
            m = (
                session.query(M)
                .filter(M.metric_name==metric.metric_name)
                .filter(M.datasource_name==self.datasource_name)
                .filter(Cluster.cluster_name==self.datasource.cluster_name)
                .first()
            )
            metric.datasource_name = self.datasource_name
            if not m:
                session.add(metric)
                session.commit()