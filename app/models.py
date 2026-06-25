from pydantic import BaseModel, Field
from typing import List, Optional


class DestinationBase(BaseModel):
    bind_ip: Optional[str] = None
    dest_ip: str
    dest_port: int = Field(ge=1, le=65535)
    prefix: int = Field(default=24, ge=8, le=32)
    mode: str = "unicast"

class DestinationCreate(DestinationBase):
    pass

class DestinationOut(DestinationBase):
    id: int
    source_id: int

    class Config:
        orm_mode = True


class SourceBase(BaseModel):
    name: str
    listen_ip: str = "0.0.0.0"
    listen_port: int = Field(ge=1, le=65535)
    interface: Optional[str] = None
    log_enabled: bool = False
    log_file: Optional[str] = None

class SourceCreate(SourceBase):
    destinations: List[DestinationCreate] = []

class SourceUpdate(BaseModel):
    name: Optional[str] = None
    listen_ip: Optional[str] = None
    listen_port: Optional[int] = None
    interface: Optional[str] = None
    log_enabled: Optional[bool] = None
    log_file: Optional[str] = None
    enabled: Optional[bool] = None
    destinations: Optional[List[DestinationCreate]] = None

class SourceOut(SourceBase):
    id: int
    enabled: bool
    running: bool = False
    stats_packets_in: int = 0
    stats_packets_out: int = 0
    stats_errors: int = 0
    stats_dropped: int = 0
    destinations: List[DestinationOut] = []

    class Config:
        orm_mode = True


class SourceStats(BaseModel):
    packets_in: int = 0
    packets_out: int = 0
    errors: int = 0
    dropped: int = 0
    running: bool = False
