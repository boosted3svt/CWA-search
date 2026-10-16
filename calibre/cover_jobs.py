"""Resumable cover positions and durable negative-cache records (no database)."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
from .state import atomic_text
from .client import HttpError, classify_failure, is_timeout_error
from .images import EmptyCoverResponse


class CoverJob:
    def __init__(self, settings, key, books):
        self.directory = Path(settings.catalog_cache_dir)/'cover-jobs'
        self.cursor = self.directory/(key+'.json')
        self.journal = self.directory/(key+'.jsonl')
        digest = hashlib.sha256()
        self.keys = {book.key:book.cover_url for book in books}
        for book in books:
            digest.update((book.key+'\n'+book.cover_url+'\n').encode())
        self.identity = digest.hexdigest()
        self.failures, self.pending = {}, []
        self.needs_separator = False
        if self.journal.exists():
            text = self.journal.read_text()
            self.needs_separator = bool(text) and not text.endswith('\n')
            for line in text.splitlines():
                try:
                    entry=json.loads(line)
                    if entry.get('key') in self.keys:
                        if entry.get('clear'):
                            self.failures.pop(entry['key'],None)
                        elif entry.get('url')==self.keys[entry['key']]:
                            self.failures[entry['key']]=entry
                except (ValueError,TypeError):pass
        self.reason_counts=Counter(entry['reason'] for entry in self.failures.values())
        try:state=json.loads(self.cursor.read_text())
        except (OSError,ValueError):state={}
        self.index=state.get('index',0) if state.get('identity')==self.identity and not state.get('complete') else 0
        if type(self.index) is not int or not 0 <= self.index <= len(books):self.index=0
        self.counts={name:state.get(name,0) if self.index else 0 for name in ('fetched','reused','failed','deferred')}
        self.retry_hours=settings.catalog_cover_failure_retry_hours

    def deferred(self, book):
        entry=self.failures.get(book.key,{})
        return entry.get('url')==book.cover_url and entry.get('retry_at',0)>time.time()

    def unavailable(self, book, error):
        previous=self.failures.get(book.key,{})
        attempts=previous.get('attempts',0)+1
        reason='http-'+str(error.status) if isinstance(error,HttpError) else ('timeout' if is_timeout_error(error) else classify_failure(error))
        if isinstance(error,EmptyCoverResponse):reason='empty-response'
        if str(error) in ('Unsupported cover image','Cover exceeds size limit'):reason='invalid-image'
        hours=min(168,self.retry_hours*2**min(attempts-1,4))
        if reason in ('empty-response','http-404','http-410'):hours=max(hours,168)
        entry={'key':book.key,'url':book.cover_url,'reason':reason,'attempts':attempts,
               'retry_at':time.time()+max(hours*3600,getattr(error,'retry_after',0))}
        if previous.get('reason'):self.reason_counts[previous['reason']]-=1
        self.reason_counts[reason]+=1
        self.failures[book.key]=entry;self.pending.append(entry)
        return reason

    def available(self, book):
        if book.key in self.failures:
            previous=self.failures.pop(book.key)
            self.reason_counts[previous['reason']]-=1
            self.pending.append({'key':book.key,'clear':True})

    def save(self, index, complete=False):
        self.directory.mkdir(parents=True,exist_ok=True)
        if self.pending:
            with self.journal.open('a') as handle:
                if self.needs_separator:handle.write('\n')
                for entry in self.pending:handle.write(json.dumps(entry)+'\n')
                handle.flush();os.fsync(handle.fileno())
            self.pending=[]
            self.needs_separator=False
        atomic_text(self.cursor,json.dumps({'identity':self.identity,'index':index,
                                           'complete':complete,**self.counts}))
        if complete:
            atomic_text(self.journal,''.join(json.dumps(entry)+'\n' for entry in self.failures.values()))

    def reasons(self):
        return {reason:count for reason,count in self.reason_counts.items() if count>0}
