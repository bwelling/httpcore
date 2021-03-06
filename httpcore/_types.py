"""
Type definitions for type checking purposes.
"""

from typing import List, Mapping, Optional, Tuple, Union

StrOrBytes = Union[str, bytes]
Origin = Tuple[bytes, bytes, int]
URL = Tuple[bytes, bytes, Optional[int], bytes]
Headers = List[Tuple[bytes, bytes]]
TimeoutDict = Mapping[str, Optional[float]]
