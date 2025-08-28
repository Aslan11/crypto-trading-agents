"""Shared utilities for Temporal client connection and management."""

import os
import asyncio
from typing import Optional
from temporalio.client import Client
from agents.constants import DEFAULT_TEMPORAL_ADDRESS, DEFAULT_TEMPORAL_NAMESPACE
from agents.logging_utils import get_logger

logger = get_logger(__name__)

# Shared Temporal client instance
_temporal_client: Optional[Client] = None
_client_lock = asyncio.Lock()


async def get_temporal_client(
    address: Optional[str] = None,
    namespace: Optional[str] = None
) -> Client:
    """Get or create a singleton Temporal client connection.
    
    Parameters
    ----------
    address:
        Temporal server address, defaults to TEMPORAL_ADDRESS env var
    namespace:
        Temporal namespace, defaults to TEMPORAL_NAMESPACE env var
        
    Returns
    -------
    Client
        Connected Temporal client instance
    """
    global _temporal_client
    
    if _temporal_client is None:
        async with _client_lock:
            # Double-check inside lock
            if _temporal_client is None:
                temporal_address = address or os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
                temporal_namespace = namespace or os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE)
                
                logger.info("Connecting to Temporal at %s (ns=%s)", temporal_address, temporal_namespace)
                _temporal_client = await Client.connect(temporal_address, namespace=temporal_namespace)
                logger.info("Temporal client ready")
                
    return _temporal_client


async def connect_temporal(
    address: Optional[str] = None,
    namespace: Optional[str] = None
) -> Client:
    """Create a new Temporal client connection (non-singleton).
    
    Parameters
    ----------
    address:
        Temporal server address, defaults to TEMPORAL_ADDRESS env var
    namespace:
        Temporal namespace, defaults to TEMPORAL_NAMESPACE env var
        
    Returns
    -------
    Client
        New Temporal client instance
    """
    temporal_address = address or os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    temporal_namespace = namespace or os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE)
    
    logger.info("Creating new Temporal connection to %s (ns=%s)", temporal_address, temporal_namespace)
    return await Client.connect(temporal_address, namespace=temporal_namespace)